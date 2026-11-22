"""Read the persisted checks and capture what still protects a natural week's packets."""

from datetime import date, timedelta
from time import monotonic
from typing import Literal

from pydantic import AwareDatetime
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import Contract, DomainError
from aris.modules.lyra.retention_service import (
    TraceRetentionBoundary,
    trace_retention_boundaries_in_transaction,
)
from aris.modules.lyra.session_occupancy import SessionOccupancyRule
from aris.modules.nova.coverage_schedule_schemas import (
    CoverageScheduleDefinition,
    WeeklyEvidenceReview,
)
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.report_calendar import weekly_report_period
from aris.modules.vega.storage_schemas import ObservedWindowStorage
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive

READ_SECONDS = 3


class WeeklyRetentionPreview(Contract):
    node_id: str
    week: date
    start_at: AwareDatetime
    end_at: AwareDatetime
    reviewed: WeeklyEvidenceReview | None = None
    observations: ObservedWindowStorage | None = None
    known_consumers: tuple[TraceRetentionBoundary, ...] = ()
    blockers: tuple[str, ...]
    remaining_checks: tuple[str, ...] = (
        "historical_build_dependencies",
        "future_report_after_retirement",
        "other_archive_consumers",
    )
    deletion_enabled: Literal[False] = False


def preview_week_retention(
    schedules: CoverageScheduleService, definition: CoverageScheduleDefinition, week: date
) -> WeeklyRetentionPreview:
    definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
    if not definition.daily_rollups:
        raise ValueError("Retention preview requires a daily coverage schedule")
    period = weekly_report_period(definition.calendar, week)
    config = definition.configuration.trace
    captured = WeeklyRetentionPreview(
        node_id=config.node_id,
        week=week,
        start_at=period.start_at,
        end_at=period.end_at,
        blockers=(),
    )
    now = schedules.clock()
    if now.utcoffset() is None:
        raise ValueError("Retention clock requires a timezone")
    if period.end_at > now:
        return captured.model_copy(update={"blockers": ("week_still_open",)})
    deadline = monotonic() + READ_SECONDS
    try:
        with schedules.sessions() as db, db.no_autoflush:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            elif db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN"))
            transaction_budget(db, 100, 1000, READ_SECONDS * 1000)
            try:
                review = schedules.evidence_review_in_transaction(db, definition, week)
            except DomainError as error:
                if error.code != "NOT_FOUND":
                    raise
                return captured.model_copy(update={"blockers": ("week_report_missing",)})
            boundary = period.end_at
            if definition.session_occupancy:
                boundary -= timedelta(seconds=SessionOccupancyRule().maximum_interruption_seconds)
            consumers = trace_retention_boundaries_in_transaction(db, config, boundary)
            archive = VegaUsageArchive(schedules.sessions)
            scope = UsageArchiveHeadQuery(agent_id=config.agent_id, node_id=config.node_id)
            observations = archive.observed_window_storage_in_transaction(
                db, scope, period.start_at, period.end_at
            )
            blockers = []
            if review.pending_days:
                blockers.append("unsatisfied_daily_evidence_checks")
            if all(consumer.consumer_id != config.consumer_id for consumer in consumers):
                blockers.append("missing_recording_consumer")
            if observations.late_arrivals:
                blockers.append("late_arrivals_need_review")
            last = observations.last_seq
            if last is not None:
                if any(last > consumer.processed_through_seq for consumer in consumers):
                    blockers.append("unprocessed_packets")
                if any(
                    consumer.checkpoint_through_seq is None
                    or last > consumer.checkpoint_through_seq
                    for consumer in consumers
                ):
                    blockers.append("week_overlaps_required_source")
                if any(
                    consumer.earliest_protected_seq is not None
                    and last >= consumer.earliest_protected_seq
                    for consumer in consumers
                ):
                    blockers.append("active_trace_or_open_run")
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Retention preview timed out", 503)
            return captured.model_copy(
                update={
                    "reviewed": review,
                    "observations": observations,
                    "known_consumers": consumers,
                    "blockers": tuple(blockers),
                }
            )
    except DBAPIError as error:
        raise DomainError(
            database_failure(error), "Retention preview is unavailable", 503
        ) from None
