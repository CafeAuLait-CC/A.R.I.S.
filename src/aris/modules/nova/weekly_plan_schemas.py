"""A prepared-week cursor is scheduling progress, never a usage or report-completion claim."""

import hashlib
from datetime import date, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from aris.core.access.schemas import AccessContract
from aris.core.scheduler.schemas import TaskQueueSummary, canonical
from aris.modules.nova.report_calendar import WeeklyReportPlan, weekly_report_period


def plan_key(plan: WeeklyReportPlan) -> str:
    return "/".join(
        (plan.namespace, plan.report_type, plan.scope.type, plan.scope.id, str(plan.owner_id))
    )


def plan_id(plan: WeeklyReportPlan) -> UUID:
    return uuid5(NAMESPACE_URL, "aris.nova.weekly-plan/" + plan_key(plan))


def plan_fingerprint(plan: WeeklyReportPlan) -> str:
    return hashlib.sha256(
        b"aris.nova.weekly-plan\x00" + canonical(plan.model_dump(mode="json")).encode()
    ).hexdigest()


class WeeklyPreparationLimits(AccessContract):
    max_weeks: int = Field(default=8, ge=1, le=8, strict=True)
    max_seconds: int = Field(default=20, ge=1, le=30, strict=True)
    lease_seconds: int = Field(default=90, ge=5, le=300, strict=True)
    transaction_timeout_ms: int = Field(default=3000, ge=1000, le=10000, strict=True)
    retry_base_seconds: int = Field(default=15, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def enough_lease(self) -> "WeeklyPreparationLimits":
        if self.lease_seconds * 1000 <= self.max_seconds * 1000 + self.transaction_timeout_ms:
            raise ValueError("weekly preparation lease must outlive its work budget")
        return self


class StoredWeeklyPlan(AccessContract):
    plan: WeeklyReportPlan
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    next_week: date
    next_due_at: AwareDatetime
    lease_token: UUID | None = Field(default=None, repr=False)
    lease_expires_at: AwareDatetime | None = None
    last_prepared_at: AwareDatetime | None = None
    last_error_code: str | None = None
    retry_at: AwareDatetime | None = None
    consecutive_failures: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def coherent(self) -> "StoredWeeklyPlan":
        if (
            self.next_due_at != weekly_report_period(self.plan, self.next_week).scheduled_for
            or self.updated_at < self.created_at
            or (self.lease_token is None) != (self.lease_expires_at is None)
            or (self.lease_expires_at is not None and self.lease_expires_at <= self.updated_at)
            or (self.last_prepared_at is not None and self.last_prepared_at > self.updated_at)
            or (self.last_error_code is None) != (self.retry_at is None)
            or (self.last_error_code is None) != (self.consecutive_failures == 0)
        ):
            raise ValueError("weekly plan progress is inconsistent")
        return self


class WeeklyPlanClaim(AccessContract):
    plan: WeeklyReportPlan
    next_week: date
    lease_token: UUID = Field(repr=False)
    expires_at: AwareDatetime


class WeeklyPlanTick(AccessContract):
    state: Literal["idle", "busy", "prepared", "retry", "lost_claim"]
    plan_id: UUID | None = None
    task_ids: tuple[UUID, ...] = ()
    next_week: date | None = None
    next_due_at: datetime | None = None
    caught_up: bool = False
    error_code: str | None = None


class WeeklyPlanView(AccessContract):
    id: UUID
    name: str
    node_id: str
    report_type: Literal["usage.mine", "usage.node"]
    timezone: str
    first_week: date
    next_week: date
    next_due_at: AwareDatetime
    last_prepared_at: AwareDatetime | None
    last_error_code: str | None
    retry_at: AwareDatetime | None
    jobs: TaskQueueSummary
    cursor_meaning: Literal["tasks_prepared"] = "tasks_prepared"
    worker_health: Literal["not_assessed"] = "not_assessed"


class WeeklyPlanPage(AccessContract):
    items: tuple[WeeklyPlanView, ...]
    has_more: bool
    read_at: AwareDatetime
