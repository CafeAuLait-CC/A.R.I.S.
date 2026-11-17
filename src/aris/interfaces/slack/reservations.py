from typing import Any

from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.contracts import DomainError
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.schemas import SlackReply, SlashCommand
from aris.modules.nova.reservation_schemas import MineQuery, MineResult
from aris.modules.vega.target_schemas import GpuInventoryLabel

SLACK_PAGE_SIZE = 5


def personal_reservations(
    result: MineResult, labels: tuple[GpuInventoryLabel, ...] = ()
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

    def __init__(self, queries: PersonalReservationQueries) -> None:
        self.queries = queries

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
        return SlackReply(personal_reservations(result.page, result.labels))
