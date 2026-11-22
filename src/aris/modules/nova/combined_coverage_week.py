"""Combine independently saved node reports for a single natural week."""

from datetime import date, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aris.core.access.schemas import AccessContract, UserPrincipal
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.nova.coverage_schedule_schemas import (
    DailyCoveragePeriodResult,
    schedule_canonical,
    schedule_fingerprint,
)


class NodeCoverageWeek(AccessContract):
    node_id: str
    report_id: UUID | None
    report_fingerprint: str | None
    coverage: Literal["complete", "partial", "unknown", "missing"]
    verified_usage_microseconds: int | None = Field(ge=0)
    verified_retained_gap_microseconds: int | None = Field(ge=0)


class CombinedCoverageWeek(AccessContract):
    week: date
    timezone: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    nodes: tuple[NodeCoverageWeek, ...] = Field(min_length=1, max_length=8)
    coverage: Literal["complete", "partial", "unknown"]
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)
    verified_retained_gap_microseconds: int | None = Field(ge=0)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    quota_usage_available: Literal[False] = False


def combine_coverage_week(
    week: date, timezone: str, reports: dict[str, DailyCoveragePeriodResult | None]
) -> CombinedCoverageWeek:
    UserPrincipal.iana_timezone(timezone)
    if week.weekday() != 0 or not 1 <= len(reports) <= 8:
        raise ValueError("Combined weekly usage needs one Monday week and one to eight nodes")
    boundaries = calendar_boundaries(week, week + timedelta(days=6), timezone)
    start, end = boundaries[0], boundaries[-1]
    nodes = []
    for node_id, result in sorted(reports.items()):
        if result is None:
            nodes.append(
                NodeCoverageWeek(
                    node_id=node_id,
                    report_id=None,
                    report_fingerprint=None,
                    coverage="missing",
                    verified_usage_microseconds=None,
                    verified_retained_gap_microseconds=None,
                )
            )
            continue
        result = DailyCoveragePeriodResult.model_validate(result.model_dump(mode="json"))
        report = result.report
        if (report.query.node_id, report.query.timezone, result.week) != (
            node_id,
            timezone,
            week,
        ) or (report.start_at, report.end_at) != (start, end):
            raise ValueError("Node report differs from the combined calendar")
        nodes.append(
            NodeCoverageWeek(
                node_id=node_id,
                report_id=result.id,
                report_fingerprint=result.fingerprint,
                coverage=report.coverage,
                verified_usage_microseconds=report.verified_usage_microseconds,
                verified_retained_gap_microseconds=report.session_evidence.retained_microseconds
                if report.session_evidence is not None
                else None,
            )
        )
    complete = all(node.coverage == "complete" for node in nodes)
    present = any(node.coverage in ("complete", "partial") for node in nodes)
    verified = sum(node.verified_usage_microseconds or 0 for node in nodes)
    sessions = [
        node.verified_retained_gap_microseconds
        for node in nodes
        if node.verified_retained_gap_microseconds is not None
    ]
    combined = CombinedCoverageWeek(
        week=week,
        timezone=timezone,
        start_at=start,
        end_at=end,
        nodes=tuple(nodes),
        coverage="complete" if complete else "partial" if present else "unknown",
        verified_usage_microseconds=verified,
        total_usage_microseconds=verified if complete else None,
        verified_retained_gap_microseconds=sum(sessions) if sessions else None,
        fingerprint="0" * 64,
    )
    return combined.model_copy(
        update={
            "fingerprint": schedule_fingerprint(
                "combined_coverage_week", schedule_canonical(combined, exclude={"fingerprint"})
            )
        }
    )
