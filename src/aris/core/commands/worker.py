from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.intake import CommandIntakeService, digest, normalized_failure
from aris.core.commands.models import CommandJob, CommandRecord
from aris.core.commands.queue_repo import CommandQueueRepository
from aris.core.commands.queue_schemas import (
    CommandClaim,
    CommandWorkerLimits,
    CommandWorkResult,
    DurableActorContext,
)
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.time import utc

TerminalState = Literal["succeeded", "failed", "expired", "cancelled"]


class CommandWorker:
    """Database actions only; external delivery belongs to the Outbox relay."""

    def __init__(self, intake: CommandIntakeService, limits: CommandWorkerLimits) -> None:
        self.intake, self.commands, self.sessions, self.limits = (
            intake,
            intake.commands,
            intake.sessions,
            limits,
        )

    def _budget(self, session: Session, deadline: datetime | None = None) -> None:
        transaction_ms = self.limits.transaction_timeout_ms
        if deadline is not None:
            remaining = int((deadline - self.commands._now()).total_seconds() * 1000)
            if remaining < 4:
                raise DomainError("EXECUTION_EXPIRED", "Execution deadline elapsed", 409)
            transaction_ms = min(transaction_ms, remaining)
        statement_ms = min(self.limits.statement_timeout_ms, transaction_ms - 1)
        lock_ms = min(self.limits.lock_timeout_ms, statement_ms - 1)
        transaction_budget(session, lock_ms, statement_ms, transaction_ms)

    @staticmethod
    def _finish(
        job: CommandJob, state: TerminalState, now: datetime, code: str | None = None
    ) -> CommandWorkResult:
        job.state, job.completed_at = state, now
        job.lease_token, job.lease_expires_at, job.last_error_code = None, None, code
        return CommandWorkResult(job_id=job.id, command_id=job.command_id, state=state, code=code)

    def _context(self, session: Session, record: CommandRecord) -> DurableActorContext:
        admission = CommandQueueRepository(session).admission(record.id)
        if admission is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Command admission is missing", 503)
        return DurableActorContext(
            actor_id=record.actor_id,
            request_id=record.request_id,
            subject={
                "provider": admission.provider,
                "namespace": admission.namespace,
                "external_id": admission.external_id,
            },
        )

    def _terminal(
        self, session: Session, job: CommandJob, record: CommandRecord
    ) -> CommandWorkResult | None:
        if record.state in {"succeeded", "expired", "cancelled", "failed"}:
            if job.state == record.state:
                return CommandWorkResult(
                    job_id=job.id,
                    command_id=record.id,
                    state=cast(TerminalState, record.state),
                    code=job.last_error_code,
                )
            return self._finish(
                job, cast(TerminalState, record.state), self.commands._now(), record.failure_code
            )
        return None

    def _fail_record(
        self, session: Session, job: CommandJob, record: CommandRecord, code: str
    ) -> CommandWorkResult:
        now = self.commands._now()
        state: Literal["expired", "failed"] = "expired" if code == "EXECUTION_EXPIRED" else "failed"
        record.state, record.completed_at, record.failure_code = state, now, code
        self.intake.audit(
            session,
            self._context(session, record),
            "command." + state,
            record.id,
            code=code,
            attempt=job.attempts,
        )
        return self._finish(job, state, now, code)

    def _eligible(self, session: Session, record: CommandRecord) -> bool:
        return record.command_type in self.commands.commands and (
            CommandQueueRepository(session).command_namespace(record.id)
            == (self.intake.namespace.provider, self.intake.namespace.namespace)
        )

    def claim(self) -> CommandClaim | CommandWorkResult | None:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = CommandQueueRepository(session)
                job = repo.next_job(
                    self.commands._now(),
                    command_types=tuple(self.commands.commands),
                    namespace=self.intake.namespace,
                )
                if job is None:
                    return None
                record = repo.command(job.command_id, lock=True)
                if record is None:
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "Job command is missing", 503)
                if not self._eligible(session, record):
                    return None
                terminal = self._terminal(session, job, record)
                if terminal is not None:
                    return terminal
                now = self.commands._now()
                if record.execution_deadline is None or record.state != "queued":
                    return self._fail_record(session, job, record, "INVALID_ADMISSION")
                if utc(record.execution_deadline) <= now:
                    return self._fail_record(session, job, record, "EXECUTION_EXPIRED")
                if job.attempts >= self.limits.max_attempts:
                    return self._fail_record(session, job, record, "ATTEMPTS_EXHAUSTED")
                job.attempts += 1
                job.state, job.lease_token = "running", str(uuid4())
                job.lease_expires_at = now + timedelta(seconds=self.limits.lease_seconds)
                self.intake.audit(
                    session,
                    self._context(session, record),
                    "command.claimed",
                    record.id,
                    attempt=job.attempts,
                )
                return CommandClaim(
                    job_id=job.id,
                    command_id=record.id,
                    lease_token=job.lease_token,
                    lease_expires_at=job.lease_expires_at,
                    attempt=job.attempts,
                )
        except DBAPIError:
            raise DomainError("WORKER_UNAVAILABLE", "Job claim outcome unavailable", 503) from None

    def _owned(self, job: CommandJob, claim: CommandClaim) -> bool:
        return (
            job.command_id == str(claim.command_id)
            and job.state == "running"
            and job.lease_token == str(claim.lease_token)
            and job.attempts == claim.attempt
            and job.lease_expires_at is not None
            and utc(job.lease_expires_at) == utc(claim.lease_expires_at)
            and utc(job.lease_expires_at) > self.commands._now()
        )

    @staticmethod
    def _lost(claim: CommandClaim) -> CommandWorkResult:
        return CommandWorkResult(
            job_id=claim.job_id, command_id=claim.command_id, state="lost_claim"
        )

    def execute(self, claim: CommandClaim) -> CommandWorkResult:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = CommandQueueRepository(session)
                job = repo.job(str(claim.job_id), lock=True)
                if job is None or job.command_id != str(claim.command_id):
                    return self._lost(claim)
                record = repo.command(job.command_id, lock=True)
                if record is None:
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "Job command is missing", 503)
                if not self._eligible(session, record):
                    return self._lost(claim)
                terminal = self._terminal(session, job, record)
                if terminal is not None:
                    return terminal
                if not self._owned(job, claim):
                    return self._lost(claim)
                deadline = record.execution_deadline
                if record.state != "queued" or deadline is None:
                    raise DomainError("INVALID_ADMISSION", "Command is not queued", 409)
                deadline = utc(deadline)
                self._budget(session, deadline)
                context = self._context(session, record)
                admission, definition, action, principal, request = self.intake.current_request(
                    session, context, record
                )
                if (
                    admission.verified_at is None
                    or admission.execution_deadline is None
                    or admission.verified_fingerprint != record.fingerprint
                    or utc(admission.execution_deadline) != deadline
                    or admission.confirmation_required != definition.requires_confirmation
                    or admission.verification_kind
                    != ("proof" if definition.requires_confirmation else "not_required")
                    or digest(definition.model_dump()) != admission.definition_fingerprint
                ):
                    raise DomainError("INVALID_ADMISSION", "Verified intent differs", 409)

                def check_deadline() -> None:
                    if self.commands._now() >= deadline:
                        raise DomainError("EXECUTION_EXPIRED", "Execution deadline elapsed", 409)
                    if self.commands._now() >= utc(claim.lease_expires_at):
                        raise DomainError("LEASE_EXPIRED", "Execution lease elapsed", 409)

                def before_commit(active: Session) -> None:
                    # Only the final transaction can publish a result. Nested
                    # savepoint releases are checked too, without owning commit.
                    check_deadline()

                def connection_commit(connection: Connection) -> None:
                    check_deadline()

                connection = session.connection()
                event.listen(session, "before_commit", before_commit)
                event.listen(connection, "commit", connection_commit)
                try:
                    check_deadline()
                    self.commands._complete(
                        session,
                        context,
                        record,
                        action,
                        principal,
                        request.parameters,
                        execution_deadline=deadline,
                    )
                    check_deadline()
                    result = self._finish(job, "succeeded", self.commands._now())
                    session.flush()
                    check_deadline()
                    commit_owned_transaction(session)
                    return result
                finally:
                    # Session/connection are private to this one worker attempt.
                    # Removing handlers also allows rollback after a failed guard.
                    for target, name, listener in (
                        (session, "before_commit", before_commit),
                        (connection, "commit", connection_commit),
                    ):
                        if event.contains(target, name, listener):
                            event.remove(target, name, listener)
        except DBAPIError as error:
            return self._recover(claim, database_failure(error))
        except DomainError as error:
            return self._recover(claim, normalized_failure(error)[0])
        except Exception:
            return self._recover(claim, "INTERNAL_ERROR")

    def _recover(self, claim: CommandClaim, code: str) -> CommandWorkResult:
        # This runs only after the complete business transaction has rolled back
        # or its commit outcome is uncertain; no partial action work is retained.
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = CommandQueueRepository(session)
                job = repo.job(str(claim.job_id), lock=True)
                if job is None or job.command_id != str(claim.command_id):
                    return self._lost(claim)
                record = repo.command(job.command_id, lock=True)
                if record is None:
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "Job command is missing", 503)
                if not self._eligible(session, record):
                    return self._lost(claim)
                terminal = self._terminal(session, job, record)
                if terminal is not None:
                    return terminal
                if not self._owned(job, claim):
                    return self._lost(claim)
                now = self.commands._now()
                if record.execution_deadline is None or utc(record.execution_deadline) <= now:
                    return self._fail_record(session, job, record, "EXECUTION_EXPIRED")
                delay = min(60, self.limits.retry_base_seconds * 2 ** (job.attempts - 1))
                if (
                    code == "RETRYABLE_DATABASE_ERROR"
                    and job.attempts < self.limits.max_attempts
                    and now + timedelta(seconds=delay) < utc(record.execution_deadline)
                ):
                    job.state, job.lease_token, job.lease_expires_at = "queued", None, None
                    job.available_at = now + timedelta(seconds=delay)
                    job.last_error_code = code
                    self.intake.audit(
                        session,
                        self._context(session, record),
                        "command.retry_scheduled",
                        record.id,
                        code=code,
                        attempt=job.attempts,
                    )
                    return CommandWorkResult(
                        job_id=job.id, command_id=record.id, state="retry", code=code
                    )
                return self._fail_record(session, job, record, code)
        except DBAPIError:
            raise DomainError(
                "WORKER_UNAVAILABLE", "Command outcome unavailable; preserve original keys", 503
            ) from None

    def run_once(self) -> CommandWorkResult | None:
        claimed = self.claim()
        if isinstance(claimed, CommandClaim):
            return self.execute(claimed)
        return claimed
