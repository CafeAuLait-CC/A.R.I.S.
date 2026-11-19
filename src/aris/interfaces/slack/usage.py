"""Ephemeral recorded-use summaries; no Slack client or background side effects."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.contracts import DomainError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.schemas import SlackReply, SlashCommand
from aris.interfaces.usage.personal import PersonalUsageQueries, PersonalUsageSummary, ReportPeriod


def recorded_duration(microseconds: int) -> str:
    if microseconds < 3_600_000_000:
        return f"{Decimal(microseconds) / Decimal(1_000_000):f} GPU seconds"
    hours = (Decimal(microseconds) / Decimal(3_600_000_000)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    return f"{hours:f} GPU hours"


def personal_usage_view(value: PersonalUsageSummary) -> dict[str, Any]:
    period = "Today so far" if value.period == "today" else "Week so far (Monday start)"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": plain("Your recorded GPU usage")},
        {
            "type": "section",
            "text": plain(
                f"{value.node_id} · {period}\n"
                f"{recorded_duration(value.recorded_microseconds)} recorded\n"
                f"{value.gpu_count} GPUs · {value.segment_count} recorded segments"
            ),
        },
        {
            "type": "context",
            "elements": [
                markdown(f"Window: {slack_time(value.start_at)} → {slack_time(value.end_at)}")
            ],
        },
        {
            "type": "context",
            "elements": [plain(f"Timezone: {value.timezone} · Unweighted residency time")],
        },
    ]
    if value.segment_count == 0:
        blocks.append(
            {
                "type": "section",
                "text": plain(
                    "No entries have been recorded for this window. Actual usage remains unknown."
                ),
            }
        )
    blocks.extend(
        [
            {
                "type": "section",
                "text": plain(
                    "Source coverage is unknown. "
                    "Complete actual usage and remaining quota are unavailable."
                ),
            },
            {
                "type": "context",
                "elements": [
                    plain(f"Ledger revision {value.ledger_revision}"),
                    markdown(f"Read: {slack_time(value.read_at)}"),
                ],
            },
        ]
    )
    return {
        "response_type": "ephemeral",
        "text": "Your recorded usage summary is shown below. "
        "Actual usage and quota are not assessed.",
        "blocks": blocks,
    }


class SlackUsageQueries:
    def __init__(self, queries: PersonalUsageQueries) -> None:
        self.queries = queries

    def validate_configuration(self, config: SlackSettings) -> None:
        if (
            not config.configured
            or self.queries.namespace.provider != "slack"
            or self.queries.namespace.namespace != config.team_id
        ):
            raise ValueError("Slack usage reads require the configured workspace")

    def read(self, command: SlashCommand, request_id: str) -> SlackReply:
        context = ExternalCommandActor(
            subject={
                "provider": "slack",
                "namespace": command.team_id,
                "external_id": command.user_id,
            },
            request_id=request_id,
        )
        parts = command.text.split()
        try:
            if len(parts) == 1:
                nodes = self.queries.choices(context)
                text = (
                    "Use /gpu usage NODE [today|week]. The default period is week.\n"
                    "Available nodes: " + ", ".join(nodes)
                )
                return SlackReply(
                    {
                        "response_type": "ephemeral",
                        "text": "Choose a node to view your recorded usage.",
                        "blocks": [{"type": "section", "text": plain(text)}],
                    }
                )
            if len(parts) not in (2, 3) or (
                len(parts) == 3 and parts[2].lower() not in ("today", "week")
            ):
                text = (
                    "Use /gpu usage NODE [today|week], or /gpu usage to list your available nodes."
                )
                return SlackReply(
                    {
                        "response_type": "ephemeral",
                        "text": text,
                        "blocks": [{"type": "section", "text": plain(text)}],
                    }
                )
            period: ReportPeriod = (
                "today" if len(parts) == 3 and parts[2].lower() == "today" else "week"
            )
            return SlackReply(personal_usage_view(self.queries.mine(context, parts[1], period)))
        except DomainError as error:
            text = (
                "Your Slack identity is not authorized to view personal usage on this node."
                if error.code == "PERMISSION_DENIED"
                else "The reporting window has just started. Try again shortly."
                if error.code == "WINDOW_NOT_STARTED"
                else "Your recorded usage is temporarily unavailable. Please try again."
            )
            return SlackReply(
                {
                    "response_type": "ephemeral",
                    "text": text,
                    "blocks": [{"type": "section", "text": plain(text)}],
                }
            )
