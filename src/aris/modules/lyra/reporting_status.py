"""Bounded day-report metadata for authorized report schedule readers."""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_build_repo import CoverageBuildRepository
from aris.modules.lyra.daily_rollup_repo import DailyUsageRollupRepository
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_schemas import UsageWindowQuery


class DailyReportingProgress(IntervalContract):
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    last_step_at: AwareDatetime | None


def saved_reporting_days(
    session: Session, node_id: str, snapshot_ids: tuple[UUID, ...]
) -> frozenset[UUID]:
    """Internal metadata only. The caller enforces current node report access."""
    if not 1 <= len(snapshot_ids) <= 7 or len(set(snapshot_ids)) != len(snapshot_ids):
        raise ValueError("Reporting status requires up to seven distinct day snapshots")
    return frozenset(DailyUsageRollupRepository(session).fingerprints(node_id, snapshot_ids))


def reporting_day_progress(
    session: Session,
    plan_id: UUID,
    query: UsageWindowQuery,
    configuration_fingerprint: str,
    *,
    read_at: datetime,
) -> DailyReportingProgress | None:
    """Read one committed build, with no raw observations or user usage."""
    repo = CoverageBuildRepository(session)
    plan = repo.get(plan_id)
    if plan is None:
        return None
    if (
        plan.request.query != query
        or plan.configuration.fingerprint != configuration_fingerprint
        or plan.created_at > read_at
    ):
        raise DomainError("CONFLICT", "Report progress differs from its schedule", 409)
    step = repo.latest(plan_id)
    if step is not None and (
        step.plan_id != plan_id
        or step.plan_fingerprint != plan.fingerprint
        or not plan.start_revision < step.revision <= plan.target_revision
        or step.manifest.id != plan.manifest_id(step.revision)
        or step.created_at < plan.created_at
        or step.created_at > read_at
    ):
        raise DomainError("CONFLICT", "Report step differs from its saved build", 409)
    return DailyReportingProgress(
        completed_steps=(step.revision if step else plan.start_revision) - plan.start_revision,
        total_steps=plan.target_revision - plan.start_revision,
        last_step_at=step.created_at if step else None,
    )
