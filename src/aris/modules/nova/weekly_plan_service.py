"""Short plan leases, durable Core tasks, then fenced progress; no cross-session write lock."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.scheduler.schemas import SchedulerContext
from aris.core.scheduler.service import SchedulerService
from aris.modules.nova.report_calendar import (
    WeeklyReportPlan,
    due_report_weeks,
    weekly_report_period,
)
from aris.modules.nova.report_scheduler import weekly_report_task
from aris.modules.nova.weekly_plan_repo import WeeklyPlanRepository
from aris.modules.nova.weekly_plan_schemas import (
    StoredWeeklyPlan,
    WeeklyPlanClaim,
    WeeklyPlanTick,
    WeeklyPreparationLimits,
    plan_id,
)


class WeeklyPlanService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        scheduler: SchedulerService,
        *,
        limits: WeeklyPreparationLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.scheduler, self.limits = sessions, scheduler, limits
        self.clock = clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)
        if limits is not None:
            if scheduler.limits is None or sessions.kw.get("bind") is not scheduler.sessions.kw.get(
                "bind"
            ):
                raise ValueError(
                    "weekly preparation and registered tasks require the same configured engine"
                )
            if (
                limits.lease_seconds
                <= limits.max_seconds + scheduler.limits.transaction_timeout_ms / 1000
            ):
                raise ValueError(
                    "weekly lease must outlive preparation and a Core task transaction"
                )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("weekly preparation clock requires a timezone")
        return value.astimezone(UTC)

    def _active(self) -> WeeklyPreparationLimits:
        if self.limits is None:
            raise DomainError("WEEKLY_PLANS_NOT_CONFIGURED", "Weekly plans are paused", 503)
        return WeeklyPreparationLimits.model_validate(self.limits.model_dump(mode="json"))

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Weekly plan database is unavailable",
            503,
        )

    @staticmethod
    def _validate(plan: WeeklyReportPlan) -> WeeklyReportPlan:
        plan = WeeklyReportPlan.model_validate(plan.model_dump(mode="json"))
        if plan.scope.type != "node" or plan.report_type not in ("usage.mine", "usage.node"):
            raise ValueError("weekly preparation requires an explicit recorded node report")
        return plan

    def _require(self, session: Session, plan: WeeklyReportPlan) -> None:
        self.access.require_in_transaction(session, self._permission(plan))

    @staticmethod
    def _permission(plan: WeeklyReportPlan) -> AccessRequest:
        return AccessRequest(
            user_id=plan.owner_id,
            scope=plan.scope,
            permission="usage.read_own" if plan.report_type == "usage.mine" else "usage.read_all",
        )

    def exact(self, id: UUID) -> StoredWeeklyPlan:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                value = WeeklyPlanRepository(session).get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Weekly plan not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def status(self, plan: WeeklyReportPlan) -> StoredWeeklyPlan:
        """Private operator status still checks the configured owner's current permission."""
        plan = self._validate(plan)
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                self._require(session, plan)
                value = WeeklyPlanRepository(session).get(plan_id(plan))
                if value is None:
                    raise DomainError("NOT_FOUND", "Weekly plan not found", 404)
                if value.plan != plan:
                    raise DomainError("CONFLICT", "Weekly plan identity has different input", 409)
                self._require(session, plan)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def claim(
        self, context: SchedulerContext, plan: WeeklyReportPlan
    ) -> WeeklyPlanClaim | WeeklyPlanTick:
        limits = self._active()
        plan = self._validate(plan)
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        id = plan_id(plan)
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1500, limits.transaction_timeout_ms)
                repo = WeeklyPlanRepository(session)
                repo.lock_identity(id)
                stored = repo.get(id, lock=True)
                now = self._now()
                new = stored is None
                if stored is None:
                    self._require(session, plan)
                    repo.append(plan, now)
                    self._audit(session, context, plan, "registered", now, revision=1)
                    session.flush()
                    stored = repo.get(id)
                    assert stored is not None
                if stored.plan != plan:
                    raise DomainError("CONFLICT", "Weekly plan identity has different input", 409)
                if now < stored.updated_at:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Weekly plan clock moved backwards", 503
                    )
                if stored.lease_expires_at is not None and stored.lease_expires_at > now:
                    return WeeklyPlanTick(
                        state="busy",
                        plan_id=id,
                        next_week=stored.next_week,
                        next_due_at=stored.next_due_at,
                    )
                if stored.next_due_at > now or (
                    stored.retry_at is not None and stored.retry_at > now
                ):
                    if new:
                        commit_owned_transaction(session)
                    return WeeklyPlanTick(
                        state="idle",
                        plan_id=id,
                        next_week=stored.next_week,
                        next_due_at=stored.next_due_at,
                        caught_up=stored.next_due_at > now,
                    )
                token = uuid4()
                expires = now + timedelta(seconds=limits.lease_seconds)
                repo.update(id, lease_token=str(token), lease_expires_at=expires, updated_at=now)
                commit_owned_transaction(session)
                return WeeklyPlanClaim(
                    plan=plan, next_week=stored.next_week, lease_token=token, expires_at=expires
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    def _owned(
        self, stored: StoredWeeklyPlan | None, claim: WeeklyPlanClaim, now: datetime
    ) -> bool:
        return bool(
            stored is not None
            and stored.plan == claim.plan
            and stored.next_week == claim.next_week
            and stored.lease_token == claim.lease_token
            and stored.lease_expires_at == claim.expires_at
            and claim.expires_at > now
            and now >= stored.updated_at
        )

    def prepare_claim(self, context: SchedulerContext, claim: WeeklyPlanClaim) -> WeeklyPlanTick:
        limits = self._active()
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        claim = WeeklyPlanClaim.model_validate(claim.model_dump(mode="json"))
        plan = self._validate(claim.plan)
        id = plan_id(plan)
        deadline = monotonic() + limits.max_seconds
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                if not self._owned(WeeklyPlanRepository(session).get(id), claim, self._now()):
                    return WeeklyPlanTick(state="lost_claim", plan_id=id)
                self._require(session, plan)
            batch = due_report_weeks(
                plan, now=self._now(), from_week=claim.next_week, limit=limits.max_weeks
            )
            ids = []
            for period in batch.periods:
                if monotonic() >= deadline or self._now() >= claim.expires_at:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Weekly preparation budget elapsed", 503
                    )
                task = weekly_report_task(plan, period)
                self.scheduler.enqueue(context, task)
                ids.append(task.id)
            if not ids or monotonic() >= deadline:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Weekly preparation did not finish its batch", 503
                )
            with self.sessions() as session:
                transaction_budget(session, 1000, 1500, limits.transaction_timeout_ms)
                repo = WeeklyPlanRepository(session)
                stored = repo.get(id, lock=True)
                now = self._now()
                if not self._owned(stored, claim, now):
                    return WeeklyPlanTick(state="lost_claim", plan_id=id)
                assert stored is not None
                self._require(session, plan)
                due = weekly_report_period(plan, batch.next_week).scheduled_for
                repo.update(
                    id,
                    next_week=batch.next_week,
                    next_due_at=due,
                    updated_at=now,
                    last_prepared_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=None,
                    retry_at=None,
                    consecutive_failures=0,
                    revision=stored.revision + 1,
                )
                self._audit(
                    session,
                    context,
                    plan,
                    "prepared",
                    now,
                    from_week=claim.next_week.isoformat(),
                    next_week=batch.next_week.isoformat(),
                    tasks=len(ids),
                    revision=stored.revision + 1,
                )
                commit_owned_transaction(session)
                return WeeklyPlanTick(
                    state="prepared",
                    plan_id=id,
                    task_ids=tuple(ids),
                    next_week=batch.next_week,
                    next_due_at=due,
                    caught_up=batch.caught_up,
                )
        except DBAPIError as error:
            return self._retry(context, claim, self._failure(error).code)
        except DomainError as error:
            code = (
                error.code
                if len(error.code) <= 64 and error.code.replace("_", "").isalnum()
                else "PLAN_PREPARATION_FAILED"
            )
            return self._retry(context, claim, code)
        except (ValueError, TypeError, RuntimeError):
            return self._retry(context, claim, "PLAN_PREPARATION_FAILED")

    def _retry(
        self, context: SchedulerContext, claim: WeeklyPlanClaim, code: str
    ) -> WeeklyPlanTick:
        limits = self._active()
        id = plan_id(claim.plan)
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1500, limits.transaction_timeout_ms)
                repo = WeeklyPlanRepository(session)
                stored = repo.get(id, lock=True)
                now = self._now()
                if not self._owned(stored, claim, now):
                    return WeeklyPlanTick(state="lost_claim", plan_id=id)
                assert stored is not None
                failures = min(10, stored.consecutive_failures + 1)
                retry = now + timedelta(
                    seconds=min(3600, limits.retry_base_seconds * 2 ** (failures - 1))
                )
                repo.update(
                    id,
                    updated_at=now,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_code=code,
                    retry_at=retry,
                    consecutive_failures=failures,
                    revision=stored.revision + 1,
                )
                self._audit(
                    session,
                    context,
                    claim.plan,
                    "retry",
                    now,
                    revision=stored.revision + 1,
                    error_code=code,
                )
                commit_owned_transaction(session)
                return WeeklyPlanTick(
                    state="retry",
                    plan_id=id,
                    next_week=stored.next_week,
                    next_due_at=stored.next_due_at,
                    error_code=code,
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    def run_once(self, context: SchedulerContext, plan: WeeklyReportPlan) -> WeeklyPlanTick:
        if self.limits is None:
            return WeeklyPlanTick(state="idle")
        result = self.claim(context, plan)
        return (
            self.prepare_claim(context, result) if isinstance(result, WeeklyPlanClaim) else result
        )

    @staticmethod
    def _audit(
        session: Session,
        context: SchedulerContext,
        plan: WeeklyReportPlan,
        action: str,
        now: datetime,
        revision: int,
        **details: str | int,
    ) -> None:
        event_id = str(uuid4())
        id = str(plan_id(plan))
        payload = {"plan_id": id, **details}
        kind = "weekly_plan." + action
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=kind,
                event_version=1,
                aggregate_type="weekly_report_plan",
                aggregate_id=id,
                aggregate_version=revision,
                occurred_at=now,
                recorded_at=now,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=id,
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=now))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=kind,
                target=id,
                request_id=context.request_id,
                recorded_at=now,
                changes=payload,
            )
        )
