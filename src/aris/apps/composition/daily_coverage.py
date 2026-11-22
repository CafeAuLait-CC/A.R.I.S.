"""One finite step toward a week composed of saved day windows."""

from collections.abc import Callable
from datetime import date, datetime

from pydantic import JsonValue
from sqlalchemy.orm import Session, sessionmaker

from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.weekly_from_daily import (
    DailyWindowReference,
    WeeklyFromDailyQuery,
    WeeklyFromDailyReader,
)
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.coverage_schedule_schemas import CoverageScheduleDefinition
from aris.modules.nova.daily_coverage_calendar import daily_coverage_targets


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
    reader = UsageWindowReader(sessions)
    reports = WeeklyFromDailyReader(reader)
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
        completed = reader.node_snapshot_fingerprints(
            actor, calendar.scope.id, (current.snapshot_id,)
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
        reports.stored_references(actor, calendar.scope.id, (current.snapshot_id,))
    return {
        **report,
        "state": "day_saved" if step["state"] == "finished" else step["state"],
        "build": step,
    }
