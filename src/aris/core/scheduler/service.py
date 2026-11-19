"""Registered database tasks with durable attempts; no implicit daemon or external executor."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, TypeAdapter
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessContract, AccessRequest
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.scheduler.repo import SchedulerRepository, inconsistent
from aris.core.scheduler.schemas import (
    MAX_TASK_BYTES,
    ScheduledTask,
    SchedulerContext,
    SchedulerLimits,
    StoredScheduledTask,
    StoredTaskRun,
    TaskClaim,
    TaskEnqueueResult,
    TaskQueueQuery,
    TaskQueueSummary,
    TaskTick,
    canonical,
)


class TaskDefinition(AccessContract):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$", max_length=128)
    permission: str = Field(min_length=1, max_length=128)
    scope_type: Literal["global", "node", "project"]


class DatabaseTaskHandler(Protocol):
    def validate(self, request: ScheduledTask) -> None: ...
    def execute_in_transaction(
        self, session: Session, request: ScheduledTask, deadline: float
    ) -> dict[str, JsonValue]: ...


_result = TypeAdapter(dict[str, JsonValue])


class SchedulerService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        registrations: tuple[tuple[TaskDefinition, DatabaseTaskHandler], ...] = (),
        *,
        limits: SchedulerLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.limits = sessions, limits
        self.clock = clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)
        self.registry = {
            definition.kind: (definition, handler) for definition, handler in registrations
        }
        if len(self.registry) != len(registrations):
            raise ValueError("duplicate scheduled task kind")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock requires timezone")
        return value.astimezone(UTC)

    def _active(self) -> SchedulerLimits:
        if self.limits is None:
            raise DomainError("SCHEDULER_NOT_CONFIGURED", "Scheduled tasks are paused", 503)
        return SchedulerLimits.model_validate(self.limits.model_dump(mode="json"))

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Scheduler database is unavailable",
            503,
        )

    @staticmethod
    def _stored(repo: SchedulerRepository, id: UUID) -> StoredScheduledTask:
        job = repo.get(id)
        if job is None:
            raise DomainError("NOT_FOUND", "Scheduled task not found", 404)
        if job.run_id is not None:
            run = repo.run(job.run_id)
            if (
                run is None
                or run.job_id != id
                or run.attempt != job.attempts
                or (
                    job.state == "running"
                    and (
                        run.state != "running"
                        or run.lease_token != job.lease_token
                        or run.lease_expires_at != job.lease_expires_at
                    )
                )
                or (
                    job.state != "running"
                    and run.state != job.state
                    and not (
                        job.state == "dead"
                        and job.error_code == "ATTEMPTS_EXHAUSTED"
                        and run.state in ("expired", "retry")
                    )
                )
            ):
                raise inconsistent()
            if job.state == "completed" and run.result != job.result:
                raise inconsistent()
            if run.state == job.state and run.error_code != job.error_code:
                raise inconsistent()
        return job

    def exact(self, id: UUID) -> StoredScheduledTask:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                return self._stored(SchedulerRepository(session), id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def runs(self, id: UUID) -> tuple[StoredTaskRun, ...]:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                self._stored(SchedulerRepository(session), id)
                return SchedulerRepository(session).runs(id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def queue_in_transaction(
        self, session: Session, query: TaskQueueQuery, *, now: datetime
    ) -> TaskQueueSummary:
        """Internal scoped metadata only; registered-task execution is not required for reads."""
        query = TaskQueueQuery.model_validate(query.model_dump(mode="json"))
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("queue status requires an aware read time")
        try:
            transaction_budget(session, 100, 1000, 5000)
            return SchedulerRepository(session).queue(query, now.astimezone(UTC))
        except DBAPIError as error:
            raise self._failure(error) from None

    def queue(self, query: TaskQueueQuery) -> TaskQueueSummary:
        with self.sessions() as session:
            return self.queue_in_transaction(session, query, now=self._now())

    def _require(self, session: Session, job: ScheduledTask, definition: TaskDefinition) -> None:
        if definition.scope_type != job.scope.type:
            raise DomainError(
                "VALIDATION_ERROR", "Task scope differs from its registered kind", 422
            )
        self.access.require_in_transaction(
            session,
            AccessRequest(user_id=job.owner_id, permission=definition.permission, scope=job.scope),
        )

    def enqueue(self, context: SchedulerContext, request: ScheduledTask) -> TaskEnqueueResult:
        limits = self._active()
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        request = ScheduledTask.model_validate(request.model_dump(mode="json"))
        registration = self.registry.get(request.kind)
        if registration is None:
            raise DomainError("SCHEDULER_NOT_CONFIGURED", "Task kind is not registered", 503)
        definition, handler = registration
        handler.validate(request)
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo = SchedulerRepository(session)
                repo.lock_key(request.key)
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request:
                        raise DomainError("CONFLICT", "Task key has different input", 409)
                    return TaskEnqueueResult(job=self._stored(repo, request.id), duplicate=True)
                self._require(session, request, definition)
                now = self._now()
                repo.append(request, now)
                session.flush()
                self._audit(session, context, request, "pending", 0, now)
                commit_owned_transaction(session)
                return TaskEnqueueResult(job=self._stored(repo, request.id), duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None

    def claim(self, context: SchedulerContext) -> TaskClaim | TaskTick | None:
        limits = self._active()
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        if not self.registry:
            return None
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo = SchedulerRepository(session)
                now = self._now()
                candidate = repo.candidate(tuple(self.registry), now)
                if candidate is None:
                    return None
                job = self._stored(repo, candidate.request.id)
                if now < job.updated_at:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Scheduler clock moved backwards", 503
                    )
                if job.state == "running":
                    assert job.run_id is not None
                    repo.finish_run(job.run_id, "expired", now, error="LEASE_EXPIRED")
                if job.attempts >= limits.max_attempts:
                    repo.update(
                        job.request.id,
                        state="dead",
                        updated_at=now,
                        lease_token=None,
                        lease_expires_at=None,
                        error_code="ATTEMPTS_EXHAUSTED",
                    )
                    self._audit(
                        session,
                        context,
                        job.request,
                        "dead",
                        job.attempts,
                        now,
                        "ATTEMPTS_EXHAUSTED",
                    )
                    commit_owned_transaction(session)
                    return TaskTick(
                        state="dead", job_id=job.request.id, error_code="ATTEMPTS_EXHAUSTED"
                    )
                token, run_id = uuid4(), uuid4()
                expires = now + timedelta(seconds=limits.lease_seconds)
                repo.append_run(run_id, job, token, now, expires)
                repo.update(
                    job.request.id,
                    state="running",
                    attempts=job.attempts + 1,
                    updated_at=now,
                    last_run_id=str(run_id),
                    lease_token=str(token),
                    lease_expires_at=expires,
                    error_code=None,
                )
                self._audit(session, context, job.request, "running", job.attempts + 1, now)
                session.flush()
                commit_owned_transaction(session)
                return TaskClaim(
                    job_id=job.request.id,
                    run_id=run_id,
                    lease_token=token,
                    attempt=job.attempts + 1,
                    expires_at=expires,
                    request_fingerprint=job.request.fingerprint,
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    def execute(self, context: SchedulerContext, claim: TaskClaim) -> TaskTick:
        limits = self._active()
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        claim = TaskClaim.model_validate(claim.model_dump(mode="json"))
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo = SchedulerRepository(session)
                repo.get(claim.job_id, lock=True)
                job = self._stored(repo, claim.job_id)
                now = self._now()
                if (
                    job.state != "running"
                    or job.run_id != claim.run_id
                    or job.lease_token != claim.lease_token
                    or job.attempts != claim.attempt
                    or job.request.fingerprint != claim.request_fingerprint
                    or job.lease_expires_at != claim.expires_at
                    or claim.expires_at <= now
                ):
                    return TaskTick(state="lost_claim", job_id=claim.job_id)
                registration = self.registry.get(job.request.kind)
                if registration is None:
                    return TaskTick(
                        state="lost_claim", job_id=claim.job_id, error_code="KIND_NOT_REGISTERED"
                    )
                definition, handler = registration
                deadline = monotonic() + limits.transaction_timeout_ms / 1000
                result = None
                code = None
                state = "completed"
                try:
                    with transaction_savepoint(session):
                        self._require(session, job.request, definition)
                        handler.validate(job.request)
                        result = _result.validate_python(
                            handler.execute_in_transaction(session, job.request, deadline)
                        )
                        if job.request.fingerprint != claim.request_fingerprint:
                            raise DomainError("VALIDATION_ERROR", "Task input was modified", 422)
                        if len(canonical(result).encode()) > MAX_TASK_BYTES:
                            raise DomainError(
                                "VALIDATION_ERROR", "Task result exceeds its budget", 422
                            )
                        self._require(session, job.request, definition)
                        if monotonic() >= deadline:
                            raise DomainError(
                                "RETRYABLE_DATABASE_ERROR", "Task execution budget exceeded", 503
                            )
                        if self._now() >= claim.expires_at:
                            raise DomainError("RETRYABLE_DATABASE_ERROR", "Task lease elapsed", 503)
                except DomainError as error:
                    code = (
                        error.code
                        if len(error.code) <= 64 and error.code.replace("_", "").isalnum()
                        else "HANDLER_REJECTED"
                    )
                    state = (
                        "retry"
                        if code in ("RETRYABLE_DATABASE_ERROR", "DEPENDENCY_UNAVAILABLE")
                        else "blocked"
                    )
                    result = None
                except (ValueError, TypeError, RuntimeError):
                    code, state, result = "HANDLER_FAILED", "retry", None
                if state == "retry" and job.attempts >= limits.max_attempts:
                    state = "dead"
                now = self._now()
                if now < job.updated_at:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Scheduler clock moved backwards", 503
                    )
                available = (
                    now
                    + timedelta(
                        seconds=min(3600, limits.retry_base_seconds * 2 ** (job.attempts - 1))
                    )
                    if state == "retry"
                    else job.available_at
                )
                repo.finish_run(claim.run_id, state, now, result=result, error=code)
                repo.update(
                    claim.job_id,
                    state=state,
                    updated_at=now,
                    available_at=available,
                    lease_token=None,
                    lease_expires_at=None,
                    canonical_result=canonical(result) if result is not None else None,
                    error_code=code,
                )
                self._audit(session, context, job.request, state, job.attempts, now, code)
                session.flush()
                self._stored(repo, claim.job_id)
                commit_owned_transaction(session)
                return TaskTick(state=state, job_id=claim.job_id, error_code=code)
        except DBAPIError as error:
            raise self._failure(error) from None

    def run_once(self, context: SchedulerContext) -> TaskTick:
        if self.limits is None or not self.registry:
            return TaskTick(state="idle")
        claimed = self.claim(context)
        if claimed is None:
            return TaskTick(state="idle")
        if isinstance(claimed, TaskTick):
            return claimed
        return self.execute(context, claimed)

    @staticmethod
    def _audit(
        session: Session,
        context: SchedulerContext,
        request: ScheduledTask,
        state: str,
        attempt: int,
        now: datetime,
        error: str | None = None,
    ) -> None:
        action = "scheduler.task." + state
        event_id = str(uuid4())
        payload = {
            "job_id": str(request.id),
            "kind": request.kind,
            "attempt": attempt,
            "state": state,
            "error_code": error,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="scheduled_job",
                aggregate_id=str(request.id),
                aggregate_version=max(1, attempt * 2 + (0 if state == "running" else 1)),
                occurred_at=now,
                recorded_at=now,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(request.id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=now))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=str(request.id),
                request_id=context.request_id,
                recorded_at=now,
                changes=payload,
            )
        )
