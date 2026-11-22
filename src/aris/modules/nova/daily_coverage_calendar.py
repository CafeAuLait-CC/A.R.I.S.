"""Bind daily coverage work to a fixed node plan and its local calendar."""

from datetime import date, timedelta
from uuid import UUID, uuid5

from pydantic import AwareDatetime

from aris.core.access.schemas import AccessContract
from aris.modules.lyra.coverage_build_schemas import completed_coverage_window_id
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.nova.coverage_schedule_schemas import CoverageScheduleDefinition
from aris.modules.nova.report_calendar import weekly_report_period


class DailyCoverageTarget(AccessContract):
    day: date
    plan_id: UUID
    snapshot_id: UUID
    scheduled_for: AwareDatetime
    query: UsageWindowQuery


def daily_coverage_targets(
    definition: CoverageScheduleDefinition, week: date
) -> tuple[DailyCoverageTarget, ...]:
    calendar = definition.calendar
    if calendar.report_type != "usage.node":
        raise ValueError("Daily coverage requires one node-wide plan")
    period = weekly_report_period(calendar, week)
    bounds = calendar_boundaries(week, week + timedelta(days=6), calendar.timezone)
    if (bounds[0], bounds[-1]) != (period.start_at, period.end_at):
        raise ValueError("Daily bounds differ from the requested week")
    targets = []
    definition_fingerprint = definition.fingerprint
    for index, (start, end) in enumerate(zip(bounds, bounds[1:], strict=False)):
        if start == end:
            continue
        day = week + timedelta(days=index)
        plan_id = uuid5(period.id, f"aris.lyra.daily-coverage/{day}/{definition_fingerprint}")
        targets.append(
            DailyCoverageTarget(
                day=day,
                plan_id=plan_id,
                snapshot_id=completed_coverage_window_id(plan_id),
                scheduled_for=end + timedelta(minutes=calendar.delay_minutes),
                query=UsageWindowQuery(
                    node_id=calendar.scope.id,
                    timezone=calendar.timezone,
                    start_at=start,
                    end_at=end,
                ),
            )
        )
    return tuple(targets)
