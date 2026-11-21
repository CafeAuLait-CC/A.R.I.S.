"""Fair bounded preparation; the shared recording queue remains the execution authority."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingContext
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits, RecordingWorkSelection
from aris.modules.lyra.stable_dispatch_repo import StableDispatchRepository
from aris.modules.lyra.stable_dispatch_schemas import StableAutoDispatchReceipt, StableDispatchState
from aris.modules.lyra.stable_window_plan_repo import StableWindowPlanRepository
from aris.modules.lyra.stable_window_plan_schemas import (
    StableWindowDispatchRequest,
    StoredStableWindowPlan,
)
from aris.modules.lyra.stable_window_plan_service import StableWindowPlanService

_TRANSIENT = {
    "RETRYABLE_DATABASE_ERROR",
    "DEPENDENCY_UNAVAILABLE",
    "USAGE_NOT_CONFIGURED",
    "JOB_CONFIGURATION_CHANGED",
}


class StableWindowDispatcher:
    def __init__(self, plans: StableWindowPlanService) -> None:
        self.plans, self.sessions = plans, plans.sessions
        self.clock = plans.clock

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stable dispatcher clock requires a timezone")
        return value.astimezone(UTC)

    def _active(self, selection: RecordingWorkSelection) -> RecordingJobLimits | None:
        if selection.include_closed_runs or selection.stable_method_fingerprint is None:
            raise DomainError(
                "VALIDATION_ERROR",
                "Stable dispatcher requires exclusive stable work selection",
                422,
            )
        configuration, limits, writer = (
            self.plans.configuration,
            self.plans.jobs.limits,
            self.plans.jobs.stable_recorder,
        )
        if (
            configuration is None
            or limits is None
            or writer is None
            or self.plans.acceptances.configuration is None
            or writer.ledger.configuration is None
        ):
            return None
        if (
            configuration.fingerprint != selection.recording_configuration_fingerprint
            or self.plans.evidence.method.fingerprint != selection.stable_method_fingerprint
            or writer.acceptances is not self.plans.acceptances
            or writer.acceptances.evidence is not self.plans.evidence
            or writer.acceptances.configuration != configuration
            or writer.ledger.configuration != configuration
        ):
            raise DomainError("CONFLICT", "Stable dispatcher cohort differs", 409)
        return RecordingJobLimits.model_validate(limits.model_dump(mode="json"))

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(
            database_failure(error), "Stable dispatcher database is unavailable", 503
        )

    def status(self, key: str) -> StableDispatchState | None:
        if re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise DomainError("VALIDATION_ERROR", "Invalid stable dispatch key", 422)
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                return StableDispatchRepository(session).get(key)
        except DBAPIError as error:
            raise self._failure(error) from None

    def _ready(self, plan: StoredStableWindowPlan, attempt: StableDispatchState | None) -> bool:
        if plan.next_window == plan.page.total_planned_windows:
            return False
        if attempt is None:
            return True
        now = self._now()
        if attempt.observed_plan_version > plan.version or attempt.updated_at > now:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable attempt precedes its source or clock", 503
            )
        return plan.version > attempt.observed_plan_version or (
            not attempt.blocked and attempt.available_at <= now
        )

    def dispatch_next(
        self, context: UsagePostingContext, selection: RecordingWorkSelection, *, limit: int = 1
    ) -> StableAutoDispatchReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        selection = RecordingWorkSelection.model_validate(selection.model_dump(mode="json"))
        if type(limit) is not int or not 1 <= limit <= 8:
            raise DomainError(
                "VALIDATION_ERROR", "Stable dispatch limit is outside its budget", 422
            )
        limits = self._active(selection)
        if limits is None:
            return StableAutoDispatchReceipt(state="paused")
        attempted: tuple[str, int] | None = None
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo, plans = StableDispatchRepository(session), StableWindowPlanRepository(session)
                # Never lock a plan row before its advisory gate: observe uses
                # advisory -> UPDATE, so the reverse order could deadlock.
                for key in repo.candidates(selection, self._now()):
                    if not plans.try_lock(key):
                        continue
                    plan = self.plans.progress_in_transaction(session, key)
                    prior = repo.get(key)
                    if not self._ready(plan, prior):
                        continue
                    attempted = (key, plan.version)
                    prefix_remaining = plan.page.stable_prefix_windows - plan.next_window
                    batch_limit = min(limit, prefix_remaining) if prefix_remaining > 0 else 1
                    result = self.plans.dispatch_in_transaction(
                        session,
                        context,
                        StableWindowDispatchRequest(
                            key=key,
                            expected_version=plan.version,
                            limit=batch_limit,
                        ),
                    )
                    if self._active(selection) != limits:
                        raise DomainError("CONFLICT", "Stable dispatch configuration changed", 409)
                    value = self._state(result.plan, prior, error=None, limits=limits)
                    self._persist(session, context, repo, value, prior, plan.event_id)
                    commit_owned_transaction(session)
                    return StableAutoDispatchReceipt(
                        state="enqueued", dispatch=result, attempt=value
                    )
                return StableAutoDispatchReceipt(state="idle")
        except DomainError as error:
            if attempted is None:
                raise
            return self._register_failure(context, selection, limits, attempted, error.code)
        except DBAPIError as error:
            if attempted is None:
                raise self._failure(error) from None
            return self._register_failure(
                context, selection, limits, attempted, database_failure(error)
            )

    def _state(
        self,
        plan: StoredStableWindowPlan,
        prior: StableDispatchState | None,
        *,
        error: str | None,
        limits: RecordingJobLimits,
    ) -> StableDispatchState:
        now = self._now()
        failures = (
            0
            if error is None
            else 1
            + (
                prior.failures
                if prior is not None and prior.observed_plan_version == plan.version
                else 0
            )
        )
        blocked = error is not None and (error not in _TRANSIENT or failures >= limits.max_attempts)
        delay = (
            min(3600, limits.retry_base_seconds * 2 ** (failures - 1))
            if failures and not blocked
            else 0
        )
        return StableDispatchState(
            key=plan.key,
            version=prior.version + 1 if prior else 1,
            observed_plan_version=plan.version,
            failures=failures,
            blocked=blocked,
            last_error_code=error,
            available_at=now + timedelta(seconds=delay),
            created_at=prior.created_at if prior else now,
            updated_at=now,
            event_id=uuid4(),
        )

    def _register_failure(
        self,
        context: UsagePostingContext,
        selection: RecordingWorkSelection,
        limits: RecordingJobLimits,
        attempted: tuple[str, int],
        code: str,
    ) -> StableAutoDispatchReceipt:
        if self._active(selection) is None:
            return StableAutoDispatchReceipt(state="paused")
        if self._active(selection) != limits:
            raise DomainError("CONFLICT", "Stable dispatch configuration changed", 409)
        code = code if re.fullmatch(r"[A-Z][A-Z_]{1,63}", code) else "PROCESSING_FAILED"
        key, version = attempted
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                plans, repo = StableWindowPlanRepository(session), StableDispatchRepository(session)
                if not plans.try_lock(key):
                    return StableAutoDispatchReceipt(state="superseded")
                plan = self.plans.progress_in_transaction(session, key)
                prior = repo.get(key)
                if plan.version != version or not self._ready(plan, prior):
                    return StableAutoDispatchReceipt(state="superseded")
                value = self._state(plan, prior, error=code, limits=limits)
                self._persist(session, context, repo, value, prior, plan.event_id)
                if self._active(selection) != limits:
                    raise DomainError("CONFLICT", "Stable dispatch configuration changed", 409)
                commit_owned_transaction(session)
                return StableAutoDispatchReceipt(
                    state="blocked" if value.blocked else "retry", attempt=value
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _persist(
        session: Session,
        context: UsagePostingContext,
        repo: StableDispatchRepository,
        value: StableDispatchState,
        prior: StableDispatchState | None,
        causation_id: UUID,
    ) -> None:
        state = "blocked" if value.blocked else "retry" if value.failures else "enqueued"
        action = "lyra.usage.stable_dispatch." + state
        payload = dict(
            layout_key=value.key,
            version=value.version,
            plan_version=value.observed_plan_version,
            failures=value.failures,
            code=value.last_error_code,
            available_at=value.available_at.isoformat(),
            quota_usage_available=False,
        )
        session.add(
            DomainEvent(
                event_id=str(value.event_id),
                event_type=action,
                event_version=1,
                aggregate_type="stable_dispatch_attempt",
                aggregate_id=value.key,
                aggregate_version=value.version,
                occurred_at=value.updated_at,
                recorded_at=value.updated_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(causation_id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=str(value.event_id), created_at=value.updated_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=value.key,
                request_id=context.request_id,
                recorded_at=value.updated_at,
                changes=payload,
            )
        )
        repo.put(value, prior)
        session.flush()
