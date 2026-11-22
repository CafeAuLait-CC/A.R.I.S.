"""Render a saved, node-level aggregate for Slack without a recipient or transport."""

from datetime import timedelta
from typing import Any

from aris.core.notifications.schemas import NoticeContent, NoticeField
from aris.interfaces.slack.notifications import notice_blocks
from aris.interfaces.slack.usage import recorded_duration
from aris.modules.nova.combined_coverage_week import CombinedCoverageWeek


def _amount(microseconds: int | None) -> str:
    return recorded_duration(microseconds) if microseconds is not None else "Unknown"


def combined_week_slack_preview(report: CombinedCoverageWeek) -> dict[str, Any]:
    sunday = report.week + timedelta(days=6)
    fields = tuple(
        NoticeField(
            label=node.node_id,
            value="No saved weekly report"
            if node.coverage == "missing"
            else (
                f"{node.coverage.capitalize()} coverage · "
                f"{_amount(node.verified_usage_microseconds)} verified"
            ),
        )
        for node in report.nodes
    )
    content = NoticeContent(
        title="ARIS weekly GPU usage",
        summary=(
            f"{report.week.isoformat()} to {sunday.isoformat()} · {report.timezone}\n"
            f"{report.coverage.capitalize()} coverage\n"
            f"Verified GPU use: {_amount(report.verified_usage_microseconds)}\n"
            f"Total GPU use: {_amount(report.total_usage_microseconds)}\n"
            f"Verified short gaps: {_amount(report.verified_retained_gap_microseconds)}"
        ),
        fields=fields,
    )
    return {
        "state": "slack_preview",
        "report_fingerprint": report.fingerprint,
        "notification_delivery": False,
        "text": content.fallback(),
        "blocks": notice_blocks(content),
    }
