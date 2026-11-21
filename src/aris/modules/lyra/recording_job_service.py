"""Recover fixed internal work; no daemon, discovery, user authority or quota activation."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.lyra.ledger_schemas import UsagePostingContext
from aris.modules.lyra.models import UsageRecordingJobRow
from aris.modules.lyra.partition_recording_schemas import PartitionRecordingReceipt
from aris.modules.lyra.partition_recording_service import PartitionRecordingService
from aris.modules.lyra.recording_job_repo import RecordingJobRepository
from aris.modules.lyra.recording_job_schemas import (
    MAX_RECORDING_JOB_INPUT_BYTES,
    RecordingJobClaim,
    RecordingJobEnqueueReceipt,
    RecordingJobLimits,
    RecordingJobResult,
    RecordingRequest,
    RecordingWorkSelection,
    StoredRecordingJob,
    recording_job_fingerprint,
    recording_request_adapter,
    recording_source_cutoff,
)
from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository
from aris.modules.lyra.run_recording_schemas import (
    ClosedRunRecordingReceipt,
    ClosedRunRecordingRequest,
)
from aris.modules.lyra.run_recording_service import ClosedRunRecordingService
from aris.modules.lyra.stable_window_acceptance_repo import StableWindowAcceptanceRepository
from aris.modules.lyra.stable_window_recording_repo import StableWindowRecordingRepository
from aris.modules.lyra.stable_window_recording_schemas import (
    StableWindowJobRequest,
    StableWindowRecordingReceipt,
)
from aris.modules.lyra.stable_window_recording_service import StableWindowRecordingService
from aris.modules.lyra.trace_consumer_schemas import canonical_trace

_TRANSIENT = {
    "RETRYABLE_DATABASE_ERROR",
    "DEPENDENCY_UNAVAILABLE",
    "USAGE_NOT_CONFIGURED",
    "JOB_CONFIGURATION_CHANGED",
}


class RecordingJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        recorder: ClosedRunRecordingService,
        *,
        limits: RecordingJobLimits | None = None,
        partition_recorder: PartitionRecordingService | None = None,
        stable_recorder: StableWindowRecordingService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.recorder = sessions, recorder
        self.partition_recorder = partition_recorder
        self.stable_recorder = stable_recorder
        self.limits = (
            RecordingJobLimits.model_validate(limits.model_dump(mode="json"))
            if limits is not None
            else None
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recording job clock requires a timezone")
        return value.astimezone(UTC)

    def _active(self) -> RecordingJobLimits:
        if self.limits is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Recording jobs are paused", 503)
        return RecordingJobLimits.model_validate(self.limits.model_dump(mode="json"))

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Recording job database is unavailable",
            503,
        )

    @staticmethod
    def _stored(repo: RecordingJobRepository, id: UUID) -> StoredRecordingJob:
        value = repo.get(id)
        if value is None:
            raise DomainError("NOT_FOUND", "Recording job not found", 404)
        if value.state == "completed":
            if isinstance(value.request, StableWindowJobRequest):
                stable_result = StableWindowRecordingRepository().get(
                    repo.session, value.request.recording
                )
                if (
                    stable_result is None
                    or stable_result.fingerprint != value.recording_fingerprint
                ):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Stable job result binding is inconsistent", 503
                    )
                return value
            result = ClosedRunRecordingRepository(repo.session).get_any(id)
            if (
                result is None
                or result.request != value.request
                or result.fingerprint != value.recording_fingerprint
            ):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Recording job result binding is inconsistent", 503
                )
        return value

    def exact(self, id: UUID) -> StoredRecordingJob:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 10000)
                return self.exact_in_transaction(session, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def exact_in_transaction(self, session: Session, id: UUID) -> StoredRecordingJob:
        """The owner supplies read budgets; pending ORM work is never flushed."""
        with session.no_autoflush:
            return self._stored(RecordingJobRepository(session), id)

    def enqueue(
        self, context: UsagePostingContext, request: RecordingRequest
    ) -> RecordingJobEnqueueReceipt:
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, 3000)
                result = self.enqueue_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise self._failure(error) from None

    def enqueue_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordingRequest
    ) -> RecordingJobEnqueueReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = recording_request_adapter.validate_python(request.model_dump(mode="json"))
        raw = canonical_trace(request)
        if len(raw.encode()) > MAX_RECORDING_JOB_INPUT_BYTES:
            raise DomainError("VALIDATION_ERROR", "Recording job input exceeds its budget", 422)
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = RecordingJobRepository(session)
                repo.lock_operation(request.id)
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request:
                        raise DomainError("CONFLICT", "Recording job ID has different input", 409)
                    return RecordingJobEnqueueReceipt(
                        job=self._stored(repo, request.id), duplicate=True
                    )
                limits = self._active()
                if isinstance(request, StableWindowJobRequest):
                    accepted = StableWindowAcceptanceRepository(session).get(request.key)
                    if accepted is None or request != StableWindowJobRequest.from_acceptance(
                        accepted
                    ):
                        raise DomainError(
                            "CONFLICT", "Stable job differs from first acceptance", 409
                        )
                now = self._now()
                if recording_source_cutoff(request) > now:
                    raise DomainError(
                        "VALIDATION_ERROR", "Recording job source cutoff is in the future", 422
                    )
                row = UsageRecordingJobRow(
                    id=str(request.id),
                    canonical_request=raw,
                    request_fingerprint=recording_job_fingerprint(request),
                    state="queued",
                    version=1,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                    available_at=now,
                )
                session.add(row)
                self._emit(session, context, row, "queued")
                session.flush()
                if self._active() != limits:
                    raise DomainError(
                        "JOB_CONFIGURATION_CHANGED", "Job configuration changed during enqueue", 503
                    )
                return RecordingJobEnqueueReceipt(
                    job=self._stored(repo, request.id), duplicate=False
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _result(job: StoredRecordingJob) -> RecordingJobResult:
        state = job.state
        if state not in {"completed", "blocked", "failed"}:
            raise ValueError("job is not terminal")
        return RecordingJobResult(
            id=job.request.id,
            state=state,
            code=job.last_error_code,
            recording_id=job.recording_id,
            recording_fingerprint=job.recording_fingerprint,
        )

    @staticmethod
    def _owned(row: UsageRecordingJobRow, claim: RecordingJobClaim, now: datetime) -> bool:
        return (
            row.id == str(claim.id)
            and row.state == "running"
            and row.version == claim.version
            and row.attempts == claim.attempt
            and row.lease_token == str(claim.lease_token)
            and row.lease_expires_at is not None
            and utc(row.updated_at) <= now
            and utc(row.lease_expires_at) == claim.lease_expires_at
            and utc(row.lease_expires_at) > now
        )

    def claim(
        self, context: UsagePostingContext, *, selection: RecordingWorkSelection | None = None
    ) -> RecordingJobClaim | RecordingJobResult | None:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        selection = (
            RecordingWorkSelection.model_validate(selection.model_dump(mode="json"))
            if selection is not None
            else None
        )
        limits = self._active()
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo = RecordingJobRepository(session)
                now = self._now()
                row = repo.next(
                    now,
                    include_partitions=self.partition_recorder is not None,
                    include_stable=self.stable_recorder is not None,
                    selection=selection,
                )
                if row is None:
                    return None
                self._stored(repo, UUID(row.id))
                if self._active() != limits:
                    raise DomainError(
                        "JOB_CONFIGURATION_CHANGED", "Job configuration changed while claiming", 503
                    )
                if now < utc(row.updated_at):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Job clock precedes its stored state", 503
                    )
                row.version += 1
                row.updated_at = now
                if row.attempts >= limits.max_attempts:
                    row.state = "failed"
                    row.last_error_code = "ATTEMPTS_EXHAUSTED"
                    row.finished_at = now
                    row.lease_token = None
                    row.lease_expires_at = None
                    self._emit(session, context, row, "failed")
                    session.flush()
                    result: RecordingJobClaim | RecordingJobResult = self._result(
                        self._stored(repo, UUID(row.id))
                    )
                else:
                    row.state = "running"
                    row.attempts += 1
                    row.lease_token = str(uuid4())
                    row.lease_expires_at = now + timedelta(seconds=limits.lease_seconds)
                    row.last_error_code = None
                    self._emit(session, context, row, "claimed")
                    session.flush()
                    result = RecordingJobClaim(
                        id=row.id,
                        version=row.version,
                        attempt=row.attempts,
                        lease_token=row.lease_token,
                        lease_expires_at=row.lease_expires_at,
                    )
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise self._failure(error) from None

    def execute(self, context: UsagePostingContext, claim: RecordingJobClaim) -> RecordingJobResult:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        claim = RecordingJobClaim.model_validate(claim.model_dump(mode="json"))
        limits = self._active()
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, limits.transaction_timeout_ms)
                repo = RecordingJobRepository(session)
                row = repo.locked(claim.id)
                if row is None:
                    return RecordingJobResult(id=claim.id, state="lost_claim")
                job = self._stored(repo, claim.id)
                if job.state == "completed":
                    return self._result(job)
                if not self._owned(row, claim, self._now()):
                    return RecordingJobResult(id=claim.id, state="lost_claim")
                result: (
                    ClosedRunRecordingReceipt
                    | PartitionRecordingReceipt
                    | StableWindowRecordingReceipt
                )
                if isinstance(job.request, ClosedRunRecordingRequest):
                    result = self.recorder.record_in_transaction(session, context, job.request)
                elif isinstance(job.request, StableWindowJobRequest):
                    if self.stable_recorder is None:
                        raise DomainError(
                            "USAGE_NOT_CONFIGURED",
                            "Stable recording handler is not configured",
                            503,
                        )
                    accepted = self.stable_recorder.acceptances.exact_in_transaction(
                        session, job.request.key
                    )
                    if job.request != StableWindowJobRequest.from_acceptance(accepted):
                        raise DomainError("CONFLICT", "Stable job metadata differs", 409)
                    result = self.stable_recorder.record_in_transaction(
                        session, context, job.request.recording
                    )
                else:
                    if self.partition_recorder is None:
                        raise DomainError(
                            "USAGE_NOT_CONFIGURED",
                            "Partition recording handler is not configured",
                            503,
                        )
                    result = self.partition_recorder.record_in_transaction(
                        session, context, job.request
                    )
                if result.id != claim.id:
                    raise DomainError("CONFLICT", "Job recorder returned another operation", 409)
                now = self._now()
                if not self._owned(row, claim, now):
                    raise DomainError(
                        "JOB_LEASE_LOST", "Recording job lease expired during execution", 409
                    )
                if self._active() != limits:
                    raise DomainError(
                        "JOB_CONFIGURATION_CHANGED",
                        "Job configuration changed during execution",
                        503,
                    )
                row.state = "completed"
                row.version += 1
                row.updated_at = now
                row.finished_at = now
                row.lease_token = None
                row.lease_expires_at = None
                row.recording_id = str(result.id)
                row.recording_fingerprint = result.fingerprint
                row.last_error_code = None
                self._emit(session, context, row, "completed")
                session.flush()
                output = self._result(self._stored(repo, claim.id))
                if self._now() >= claim.lease_expires_at:
                    raise DomainError(
                        "JOB_LEASE_LOST", "Recording job lease expired before commit", 409
                    )
                if self._active() != limits:
                    raise DomainError(
                        "JOB_CONFIGURATION_CHANGED", "Job configuration changed before commit", 503
                    )
                commit_owned_transaction(session)
                return output
        except DomainError as error:
            if error.code == "USAGE_NOT_CONFIGURED" and self.limits is None:
                raise
            return self._register_failure(context, claim, error.code, limits)
        except DBAPIError as error:
            return self._register_failure(context, claim, database_failure(error), limits)

    def _register_failure(
        self,
        context: UsagePostingContext,
        claim: RecordingJobClaim,
        code: str,
        limits: RecordingJobLimits,
    ) -> RecordingJobResult:
        code = code if re.fullmatch(r"[A-Z][A-Z_]{1,63}", code) else "PROCESSING_FAILED"
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, 3000)
                repo = RecordingJobRepository(session)
                row = repo.locked(claim.id)
                if row is None:
                    return RecordingJobResult(id=claim.id, state="lost_claim")
                job = self._stored(repo, claim.id)
                if job.state == "completed":
                    return self._result(job)
                now = self._now()
                if not self._owned(row, claim, now):
                    return RecordingJobResult(id=claim.id, state="lost_claim")
                if now < utc(row.updated_at):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Job clock precedes its stored state", 503
                    )
                row.version += 1
                row.updated_at = now
                row.lease_token = None
                row.lease_expires_at = None
                row.last_error_code = code
                if code in _TRANSIENT and row.attempts < limits.max_attempts:
                    row.state = "queued"
                    row.available_at = now + timedelta(
                        seconds=min(3600, limits.retry_base_seconds * 2 ** (row.attempts - 1))
                    )
                    output = RecordingJobResult(id=claim.id, state="retry", code=code)
                else:
                    row.state = "failed" if code in _TRANSIENT else "blocked"
                    row.finished_at = now
                    output = RecordingJobResult(id=claim.id, state=row.state, code=code)
                self._emit(session, context, row, "retry" if row.state == "queued" else row.state)
                session.flush()
                self._stored(repo, claim.id)
                if self._now() >= claim.lease_expires_at:
                    session.rollback()
                    return RecordingJobResult(id=claim.id, state="lost_claim")
                commit_owned_transaction(session)
                return output
            except DBAPIError as error:
                raise self._failure(error) from None

    @staticmethod
    def _emit(
        session: Session, context: UsagePostingContext, row: UsageRecordingJobRow, transition: str
    ) -> None:
        action = "lyra.usage.recording_job." + transition
        event_id = str(uuid4())
        payload = {
            "job_id": row.id,
            "state": row.state,
            "version": row.version,
            "attempts": row.attempts,
            "code": row.last_error_code,
            "request_fingerprint": row.request_fingerprint,
            "recording_fingerprint": row.recording_fingerprint,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="usage_recording_job",
                aggregate_id=row.id,
                aggregate_version=row.version,
                occurred_at=row.updated_at,
                recorded_at=row.updated_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=row.id,
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=row.updated_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=row.id,
                request_id=context.request_id,
                recorded_at=row.updated_at,
                changes=payload,
            )
        )
