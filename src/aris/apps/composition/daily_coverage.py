"""One finite step toward a week composed of saved day windows."""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.scheduler.schemas import SchedulerContext
from aris.modules.lyra.coverage_build_schemas import completed_coverage_window_id
from aris.modules.lyra.coverage_rollup import SavedCoverageRollup
from aris.modules.lyra.daily_session_occupancy_service import DailySessionOccupancyService
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.retention_service import daily_archive_freeze
from aris.modules.lyra.session_occupancy_service import SessionOccupancyService
from aris.modules.lyra.weekly_from_daily import (
    DailyWindowReference,
    WeeklyFromDaily,
    WeeklyFromDailyQuery,
    WeeklyFromDailyReader,
    WeeklySourceDay,
)
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow
from aris.modules.nova.coverage_schedule_schemas import (
    CoverageScheduleDefinition,
    DailyCoverageEvidence,
    DailyCoveragePeriodResult,
)
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.daily_coverage_calendar import DailyCoverageTarget, daily_coverage_targets
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from aris.modules.vega.usage_span import VegaUsageSpans


def _daily_report_reader(
    sessions: sessionmaker[Session],
    definition: CoverageScheduleDefinition,
    clock: Callable[[], datetime],
) -> WeeklyFromDailyReader:
    if not definition.session_occupancy:
        return WeeklyFromDailyReader(UsageWindowReader(sessions, clock=clock))
    return WeeklyFromDailyReader(
        UsageWindowReader(sessions, clock=clock, timeout_ms=6000),
        DailySessionOccupancyService(
            IndexedUsageHistoryService(sessions, clock=clock, timeout_ms=6000),
            SessionOccupancyService(
                VegaUsageSpans(), NodeUserHistoryService(sessions, clock=clock)
            ),
        ),
    )


def daily_coverage_step(
    sessions: sessionmaker[Session],
    definition: CoverageScheduleDefinition,
    week: date,
    *,
    clock: Callable[[], datetime],
    status_only: bool,
    prefix_proofs_per_tick: int,
    transaction_timeout_ms: int,
    include_weekly: bool = True,
) -> dict[str, JsonValue]:
    targets = daily_coverage_targets(definition, week)
    calendar = definition.calendar
    actor = ActorContext(actor_id=calendar.owner_id, request_id="daily-coverage-check")
    reports = _daily_report_reader(sessions, definition, clock)
    known = reports.stored_references(
        actor, calendar.scope.id, tuple(target.snapshot_id for target in targets)
    )
    pending = tuple(target for target in targets if target.snapshot_id not in known)
    now = clock()
    report: dict[str, JsonValue] = {
        "schedule_id": str(definition.id),
        "week": week.isoformat(),
        "saved_days": len(known),
        "expected_days": len(targets),
    }
    if not pending:
        report["state"] = "report_ready"
        if include_weekly:
            value = reports.read(
                actor,
                WeeklyFromDailyQuery(
                    node_id=calendar.scope.id, week=week, timezone=calendar.timezone
                ),
                tuple(
                    DailyWindowReference(snapshot_id=id, snapshot_fingerprint=fp)
                    for id, fp in known.items()
                ),
            )
            report["report"] = value.model_dump(mode="json")
        return report
    current = pending[0]
    report.update(day=current.day.isoformat(), due_at=current.scheduled_for.isoformat())
    if current.scheduled_for > now or status_only:
        return {**report, "state": "future_day" if current.scheduled_for > now else "pending"}
    return {
        **report,
        **_build_day(
            sessions,
            definition,
            current,
            actor,
            reports,
            clock=clock,
            prefix_proofs_per_tick=prefix_proofs_per_tick,
            transaction_timeout_ms=transaction_timeout_ms,
        ),
    }


def _build_day(
    sessions: sessionmaker[Session],
    definition: CoverageScheduleDefinition,
    current: DailyCoverageTarget,
    actor: ActorContext,
    reports: WeeklyFromDailyReader,
    *,
    clock: Callable[[], datetime],
    prefix_proofs_per_tick: int,
    transaction_timeout_ms: int,
) -> dict[str, JsonValue]:
    graph = build_lyra_coverage_components(
        sessions,
        LyraCoverageRuntimeConfig(
            enabled=True,
            configuration=definition.configuration,
            target=CoverageBuildTarget(id=current.plan_id, query=current.query),
            prepare_prefix=True,
            prefix_proofs_per_tick=prefix_proofs_per_tick,
            transaction_timeout_ms=transaction_timeout_ms,
        ),
        clock=clock,
    )
    step = graph.run_once()
    if step["state"] == "finished":
        completed = reports.daily_windows.node_snapshot_fingerprints(
            actor, current.query.node_id, (current.snapshot_id,)
        )
        fingerprint = completed.get(current.snapshot_id)
        if fingerprint is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Finished day has no saved snapshot", 503)
        reports.save_day(
            actor,
            current.query,
            DailyWindowReference(snapshot_id=current.snapshot_id, snapshot_fingerprint=fingerprint),
        )
    else:
        reports.stored_references(actor, current.query.node_id, (current.snapshot_id,))
    return {
        "state": "day_saved" if step["state"] == "finished" else step["state"],
        "build": step,
    }


def prepare_day_revision(
    schedules: CoverageScheduleService,
    definition: CoverageScheduleDefinition,
    day: date,
    revision_id: UUID,
    *,
    prefix_proofs_per_tick: int,
    transaction_timeout_ms: int,
) -> dict[str, JsonValue]:
    """Build a separate daily rollup and compare it without publishing a changed week."""
    week = day - timedelta(days=day.weekday())
    actor = ActorContext(actor_id=definition.calendar.owner_id, request_id="prepare-day-revision")
    official = schedules.verify_daily_week(
        SchedulerContext(request_id=actor.request_id), definition, week
    )
    proposed = _revision_target(definition, official, day, revision_id)
    reports = _daily_report_reader(schedules.sessions, definition, schedules.clock)
    known = reports.stored_references(actor, proposed.query.node_id, (proposed.snapshot_id,))
    result: dict[str, JsonValue] = {
        "state": "revision_ready",
        "revision_id": str(revision_id),
        "node_id": proposed.query.node_id,
        "day": day.isoformat(),
        "official_report_id": str(official.id),
        "official_report_fingerprint": official.fingerprint,
        "published": False,
    }
    if proposed.snapshot_id not in known:
        if not schedules.enabled:
            raise DomainError(
                "WEEKLY_COVERAGE_NOT_CONFIGURED", "Preparing revisions is paused", 503
            )
        return {
            **result,
            **_build_day(
                schedules.sessions,
                definition,
                proposed,
                actor,
                reports,
                clock=schedules.clock,
                prefix_proofs_per_tick=prefix_proofs_per_tick,
                transaction_timeout_ms=transaction_timeout_ms,
            ),
        }
    candidate = _revision_week(reports, actor, official, proposed, known[proposed.snapshot_id])
    return {
        **result,
        "comparison": {
            "recorded_microseconds_delta": candidate.recorded_microseconds
            - official.report.recorded_microseconds,
            "verified_usage_microseconds_delta": candidate.verified_usage_microseconds
            - official.report.verified_usage_microseconds,
            "official_coverage": official.report.coverage,
            "candidate_coverage": candidate.coverage,
        },
        "candidate": candidate.model_dump(mode="json"),
    }


def _revision_target(
    definition: CoverageScheduleDefinition,
    official: DailyCoveragePeriodResult,
    day: date,
    revision_id: UUID,
) -> DailyCoverageTarget:
    original = next((value for value in official.report.days if value.day == day), None)
    target = next(
        (value for value in daily_coverage_targets(definition, official.week) if value.day == day),
        None,
    )
    if original is None or target is None or original.snapshot_id != target.snapshot_id:
        raise DomainError("NOT_FOUND", "Saved reporting day is unavailable", 404)
    plan = uuid5(target.plan_id, "daily-revision/" + str(revision_id))
    return target.model_copy(
        update={"plan_id": plan, "snapshot_id": completed_coverage_window_id(plan)}
    )


def _revision_week(
    reports: WeeklyFromDailyReader,
    actor: ActorContext,
    official: DailyCoveragePeriodResult,
    proposed: DailyCoverageTarget,
    fingerprint: str,
) -> WeeklyFromDaily:
    return reports.read(
        actor,
        official.report.query,
        tuple(
            DailyWindowReference(snapshot_id=proposed.snapshot_id, snapshot_fingerprint=fingerprint)
            if source.day == proposed.day
            else DailyWindowReference(
                snapshot_id=source.snapshot_id, snapshot_fingerprint=source.snapshot_fingerprint
            )
            for source in official.report.days
        ),
    )


def verify_day_revision(
    schedules: CoverageScheduleService,
    definition: CoverageScheduleDefinition,
    day: date,
    revision_id: UUID,
    *,
    expected_fingerprint: str,
    transaction_timeout_ms: int,
) -> dict[str, JsonValue]:
    """Recheck one prepared revision without replacing the saved week."""
    week = day - timedelta(days=day.weekday())
    context = SchedulerContext(request_id="daily-revision-verification")
    actor = ActorContext(actor_id=definition.calendar.owner_id, request_id=context.request_id)
    official = schedules.verify_daily_week(context, definition, week)
    target = _revision_target(definition, official, day, revision_id)
    reports = _daily_report_reader(schedules.sessions, definition, schedules.clock)
    known = reports.stored_references(actor, definition.calendar.scope.id, (target.snapshot_id,))
    if target.snapshot_id not in known:
        raise DomainError("NOT_FOUND", "Prepared reporting day was not found", 404)
    candidate = _revision_week(reports, actor, official, target, known[target.snapshot_id])
    if candidate.fingerprint != expected_fingerprint:
        raise DomainError("CONFLICT", "Prepared weekly report fingerprint differs", 409)
    source = next((value for value in candidate.days if value.day == day), None)
    if source is None or candidate.missing_days:
        raise DomainError("CONFLICT", "Prepared weekly report is missing a reporting day", 409)
    snapshot, rollup = _reverify_daily_rollup(
        schedules, definition, target, source, actor, transaction_timeout_ms
    )
    freeze = daily_archive_freeze(
        snapshot, include_session_context=definition.session_occupancy is True
    )
    with schedules.sessions() as db, db.no_autoflush:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        transaction_budget(db, 100, 1000, 3000)
        (new_arrivals,) = VegaUsageArchive(
            schedules.sessions
        ).arrivals_outside_freezes_in_transaction(db, (freeze,))
        review, current = schedules.evidence_review_in_transaction(db, definition, week)
        if new_arrivals or current != official:
            raise DomainError("CONFLICT", "Prepared reporting evidence changed", 409)
    return {
        "state": "revision_verified",
        "revision_id": str(revision_id),
        "node_id": definition.calendar.scope.id,
        "day": day.isoformat(),
        "official_report_fingerprint": official.fingerprint,
        "candidate_fingerprint": candidate.fingerprint,
        "snapshot_fingerprint": snapshot.fingerprint,
        "rollup_fingerprint": rollup.fingerprint,
        "other_days_pending": [value.isoformat() for value in review.pending_days if value != day],
        "published": False,
    }


def verify_daily_coverage(
    schedules: CoverageScheduleService,
    definition: CoverageScheduleDefinition,
    day: date,
    *,
    transaction_timeout_ms: int,
    record: bool = False,
) -> dict[str, JsonValue]:
    """Reverify the original evidence for one day of a published weekly result."""
    week = day - timedelta(days=day.weekday())
    context = SchedulerContext(request_id="daily-coverage-verification")
    report = schedules.verify_daily_week(context, definition, week)
    target = next(
        (target for target in daily_coverage_targets(definition, week) if target.day == day), None
    )
    source = next((source for source in report.report.days if source.day == day), None)
    if target is None or source is None or source.snapshot_id != target.snapshot_id:
        raise DomainError("NOT_FOUND", "Published day evidence was not found", 404)
    calendar = definition.calendar
    actor = ActorContext(actor_id=calendar.owner_id, request_id=context.request_id)
    snapshot, rollup = _reverify_daily_rollup(
        schedules, definition, target, source, actor, transaction_timeout_ms
    )
    schedules.verify_daily_week(context, definition, week)
    evidence = DailyCoverageEvidence(
        report_id=report.id,
        report_fingerprint=report.fingerprint,
        day=day,
        snapshot_fingerprint=snapshot.fingerprint,
        rollup_fingerprint=rollup.fingerprint,
    )
    saved = schedules.save_evidence_check(definition, evidence) if record else None
    return {
        "schedule_id": str(definition.id),
        "node_id": calendar.scope.id,
        "week": week.isoformat(),
        **evidence.model_dump(mode="json"),
        "coverage": rollup.coverage,
        "original_evidence_reverified": True,
        "deletion_enabled": False,
        "checked_at": saved.checked_at.isoformat() if saved else None,
    }


def _reverify_daily_rollup(
    schedules: CoverageScheduleService,
    definition: CoverageScheduleDefinition,
    target: DailyCoverageTarget,
    source: WeeklySourceDay,
    actor: ActorContext,
    transaction_timeout_ms: int,
) -> tuple[StoredUsageWindow, SavedCoverageRollup]:
    graph = build_lyra_coverage_components(
        schedules.sessions,
        LyraCoverageRuntimeConfig(
            enabled=False,
            configuration=definition.configuration,
            target=CoverageBuildTarget(id=target.plan_id, query=target.query),
            transaction_timeout_ms=transaction_timeout_ms,
        ),
        clock=schedules.clock,
        status_only=True,
    )
    assert graph.service is not None
    windows = graph.service.windows
    snapshot = windows.reverify(source.snapshot_id)
    reports = _daily_report_reader(schedules.sessions, definition, schedules.clock)
    rollup = reports.project_day(actor, source.snapshot_id, node_id=target.query.node_id)
    if (
        snapshot.fingerprint != source.snapshot_fingerprint
        or snapshot.request.query != target.query
        or snapshot.request.id != target.snapshot_id
        or rollup.fingerprint != source.rollup_fingerprint
    ):
        raise DomainError("CONFLICT", "Daily rollup differs from original evidence", 409)
    return snapshot, rollup
