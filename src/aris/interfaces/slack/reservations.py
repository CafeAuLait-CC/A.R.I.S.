from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.contracts import DomainError
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.reservation_changes import CHANGE_ACTION_PREFIX
from aris.interfaces.slack.schemas import SlackReply, SlashCommand
from aris.modules.nova.reservation_schemas import MineQuery, MineResult
from aris.modules.vega.target_schemas import GpuInventoryLabel

SLACK_PAGE_SIZE = 5


def personal_reservations(
    result: MineResult,
    labels: tuple[GpuInventoryLabel, ...] = (),
    *,
    change_commands: Sequence[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    if not result.reservations:
        return {
            "response_type": "ephemeral",
            "text": "You have no reservations.",
            "blocks": [{"type": "section", "text": plain("You have no reservations.")}],
        }
    blocks: list[dict[str, Any]] = [{"type": "header", "text": plain("Your reservations")}]
    inventory = {label.gpu_uuid: label for label in labels}
    for reservation in result.reservations:
        label = inventory.get(reservation.gpu_uuid)
        resource = (
            f"{label.node_id} · GPU {label.index} · {label.model}"
            if label
            else "GPU information unavailable"
        )
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": plain(
                        f"{resource} · {reservation.status.capitalize()}\n" + reservation.purpose
                    ),
                },
                {
                    "type": "context",
                    "elements": [
                        markdown(
                            f"Scheduled: {slack_time(reservation.start_at)} → "
                            f"{slack_time(reservation.end_at)}"
                        )
                    ],
                },
                {
                    "type": "context",
                    "elements": [plain(f"Reservation: {reservation.id}")],
                },
            ]
        )
        if (
            now is not None
            and reservation.status in {"confirmed", "active"}
            and reservation.version is not None
        ):
            operations = ["cancel"] if now < reservation.start_at else ["release"]
            if now < reservation.end_at:
                operations.append("extend")
            elements = [
                {
                    "type": "button",
                    "action_id": CHANGE_ACTION_PREFIX + operation,
                    "text": plain(operation.capitalize()),
                    "value": str(reservation.id),
                }
                for operation in operations
                if "reservation." + operation in change_commands
            ]
            if elements:
                blocks.append({"type": "actions", "elements": elements})
    if result.next_cursor is not None:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    plain(
                        "More reservations are available. Showing the five most recently created."
                    )
                ],
            }
        )
    blocks.append(
        {"type": "context", "elements": [plain("Resource labels use last-known inventory.")]}
    )
    return {
        "response_type": "ephemeral",
        # Untrusted purposes and resource identifiers occur only in plain_text
        # blocks, never in a fallback that Slack might interpret as markup.
        "text": "Your reservations are listed below.",
        "blocks": blocks,
    }


class SlackReservationQueries:
    """Only invoked after raw-body Slack signature and workspace validation."""

    def __init__(
        self,
        queries: PersonalReservationQueries,
        *,
        change_commands: Sequence[str] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.queries = queries
        self.change_commands, self.clock = tuple(change_commands), clock

    def validate_configuration(self, config: SlackSettings) -> None:
        if (
            not config.configured
            or self.queries.namespace.provider != "slack"
            or self.queries.namespace.namespace != config.team_id
        ):
            raise ValueError("Slack reservation queries require the configured workspace")

    def mine(self, command: SlashCommand, request_id: str) -> SlackReply:
        try:
            result = self.queries.mine_labeled(
                ExternalCommandActor(
                    subject={
                        "provider": "slack",
                        "namespace": command.team_id,
                        "external_id": command.user_id,
                    },
                    request_id=request_id,
                ),
                MineQuery(limit=SLACK_PAGE_SIZE),
            )
        except DomainError as error:
            text = (
                "Your Slack identity is not authorized to view personal reservations."
                if error.code == "PERMISSION_DENIED"
                else "Your reservations are temporarily unavailable. Please try again."
            )
            return SlackReply(
                {
                    "response_type": "ephemeral",
                    "text": text,
                    "blocks": [{"type": "section", "text": plain(text)}],
                }
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("personal reservation clock requires a timezone")
        return SlackReply(
            personal_reservations(
                result.page, result.labels, change_commands=self.change_commands, now=now
            )
        )
