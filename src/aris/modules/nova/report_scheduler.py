"""Nova turns explicit calendar plans into registered Core tasks; no implicit schedules."""

from datetime import date, datetime

from aris.core.access.schemas import AccessContract
from aris.core.scheduler.schemas import ScheduledTask, SchedulerContext, TaskEnqueueResult
from aris.core.scheduler.service import SchedulerService
from aris.modules.lyra.weekly_report_schemas import WeeklyReportRequest
from aris.modules.lyra.weekly_report_service import WEEKLY_KINDS
from aris.modules.nova.report_calendar import (
    WeeklyReportPeriod,
    WeeklyReportPlan,
    due_report_weeks,
    weekly_report_period,
)


class WeeklyEnqueueBatch(AccessContract):
    results: tuple[TaskEnqueueResult, ...]
    next_week: date
    caught_up: bool


def weekly_report_task(plan: WeeklyReportPlan, period: WeeklyReportPeriod) -> ScheduledTask:
    if (
        plan.scope.type != "node"
        or plan.report_type not in ("usage.mine", "usage.node")
        or period != weekly_report_period(plan, period.week)
    ):
        raise ValueError("recorded weekly report task differs from its explicit plan")
    mode = "mine" if plan.report_type == "usage.mine" else "node"
    payload = WeeklyReportRequest(
        mode=mode,
        week=period.week,
        timezone=plan.timezone,
        start_at=period.start_at,
        end_at=period.end_at,
    )
    return ScheduledTask(
        key=period.key,
        kind=WEEKLY_KINDS[mode],
        owner_id=plan.owner_id,
        scope=plan.scope,
        scheduled_for=period.scheduled_for,
        payload=payload.model_dump(mode="json"),
    )


def enqueue_weekly_reports(
    scheduler: SchedulerService,
    context: SchedulerContext,
    plan: WeeklyReportPlan,
    *,
    now: datetime,
    from_week: date | None = None,
    limit: int = 8,
) -> WeeklyEnqueueBatch:
    if plan.scope.type != "node" or plan.report_type not in ("usage.mine", "usage.node"):
        raise ValueError("recorded weekly reports require a node and explicit report view")
    batch = due_report_weeks(plan, now=now, from_week=from_week, limit=limit)
    results = []
    for period in batch.periods:
        request = weekly_report_task(plan, period)
        results.append(scheduler.enqueue(context, request))
    return WeeklyEnqueueBatch(
        results=tuple(results), next_week=batch.next_week, caught_up=batch.caught_up
    )
