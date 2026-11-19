"""Explicit Monday-based report periods; elapsed UTC hours follow the chosen calendar."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.schemas import AccessContract, AccessScope
from aris.core.scheduler.schemas import task_identity


class WeeklyReportPlan(AccessContract):
    namespace: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    report_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    owner_id: UUID
    scope: AccessScope
    timezone: str = Field(min_length=1, max_length=128)
    first_week: date
    delay_minutes: int = Field(default=0, ge=0, le=10080, strict=True)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("weekly reports require an IANA timezone") from None
        return value

    @model_validator(mode="after")
    def explicit_monday(self) -> "WeeklyReportPlan":
        if self.first_week.weekday() != 0:
            raise ValueError("first report week must be a local Monday")
        return self


class WeeklyReportPeriod(AccessContract):
    week: date
    start_at: AwareDatetime
    end_at: AwareDatetime
    scheduled_for: AwareDatetime
    key: str
    id: UUID


class WeeklyReportBatch(AccessContract):
    periods: tuple[WeeklyReportPeriod, ...]
    next_week: date
    caught_up: bool


def weekly_report_period(plan: WeeklyReportPlan, week: date) -> WeeklyReportPeriod:
    plan = WeeklyReportPlan.model_validate(plan.model_dump(mode="json"))
    if week.weekday() != 0 or week < plan.first_week:
        raise ValueError("report week must be an enabled local Monday")
    zone = ZoneInfo(plan.timezone)
    start = datetime.combine(week, time.min, zone).astimezone(UTC)
    end = datetime.combine(week + timedelta(days=7), time.min, zone).astimezone(UTC)
    if end <= start:
        raise ValueError("report calendar has no positive week")
    # Offset/label changes do not create a second report for the same requested period.
    key = "/".join(
        (
            plan.namespace,
            plan.report_type,
            plan.scope.type,
            plan.scope.id,
            str(plan.owner_id),
            week.isoformat(),
        )
    )
    return WeeklyReportPeriod(
        week=week,
        start_at=start,
        end_at=end,
        scheduled_for=end + timedelta(minutes=plan.delay_minutes),
        key=key,
        id=task_identity(key),
    )


def due_report_weeks(
    plan: WeeklyReportPlan, *, now: datetime, from_week: date | None = None, limit: int = 8
) -> WeeklyReportBatch:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("weekly scheduling clock requires a timezone")
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("weekly scheduling requires a bounded batch")
    week = plan.first_week if from_week is None else from_week
    if week.weekday() != 0 or week < plan.first_week:
        raise ValueError("weekly continuation must be an enabled local Monday")
    periods: list[WeeklyReportPeriod] = []
    while len(periods) < limit:
        value = weekly_report_period(plan, week)
        if value.scheduled_for > now:
            return WeeklyReportBatch(periods=tuple(periods), next_week=week, caught_up=True)
        periods.append(value)
        week += timedelta(days=7)
    return WeeklyReportBatch(
        periods=tuple(periods),
        next_week=week,
        caught_up=weekly_report_period(plan, week).scheduled_for > now,
    )
