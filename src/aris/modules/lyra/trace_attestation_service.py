"""Append bounded original-source proofs without rescanning an unbounded run."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.trace_attestation_repo import (
    TraceAttestationRepository,
    inconsistent_attestation,
)
from aris.modules.lyra.trace_attestation_schemas import (
    StoredTraceAttestation,
    TraceAttestationReceipt,
    TraceAttestationRequest,
    attestation_fingerprint,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    StoredTraceConsumption,
    UsageTraceConsumeContext,
)


class TraceRecordVerifier(Protocol):
    def verify_record_in_transaction(
        self, session: Session, record_id: UUID, expected_fingerprint: str, source_cutoff: datetime
    ) -> StoredTraceConsumption: ...


class TraceAttestationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        verifier: TraceRecordVerifier,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 10000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise ValueError("source attestation requires a bounded internal timeout")
        self.sessions, self.verifier = sessions, verifier
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("source attestation clock requires a timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Source attestation budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Source attestation database is unavailable",
            503,
        )

    def exact(self, id: UUID) -> StoredTraceAttestation:
        """Historical receipt only; this does not revalidate old original bodies."""
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                value = TraceAttestationRepository(session).get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Source attestation not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _previous(
        session: Session, source: StoredTraceConsumption, cutoff: datetime
    ) -> StoredTraceAttestation | None:
        before = source.before
        if before.record_id is None:
            if before.revision != 0:
                raise inconsistent_attestation()
            return None
        previous = TraceAttestationRepository(session).get(before.record_id, by_record=True)
        parent = UsageTraceRepository(session).get(before.record_id)
        if previous is None or parent is None:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "The preceding source segment is not attested", 503
            )
        if (
            parent.position != before
            or previous.request.expected_record_fingerprint != parent.fingerprint
            or previous.consumer_id != before.configuration.consumer_id
            or previous.configuration_fingerprint != before.configuration.fingerprint
            or previous.revision != before.revision
            or previous.last_seq != before.after_seq
            or parent.created_at > cutoff
        ):
            raise DomainError("CONFLICT", "Preceding source attestation binding differs", 409)
        return previous

    def _source(
        self, session: Session, request: TraceAttestationRequest
    ) -> tuple[StoredTraceConsumption, StoredTraceAttestation | None]:
        source = self.verifier.verify_record_in_transaction(
            session, request.record_id, request.expected_record_fingerprint, request.source_cutoff
        )
        source = StoredTraceConsumption.model_validate(source.model_dump(mode="json"))
        if (
            source.request.id != request.record_id
            or source.fingerprint != request.expected_record_fingerprint
            or source.created_at > request.source_cutoff
        ):
            raise DomainError("CONFLICT", "Source attestation verifier binding differs", 409)
        previous = self._previous(session, source, request.source_cutoff)
        if previous is not None and previous.created_at > self._now():
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Preceding attestation is not reached", 503)
        return source, previous

    @staticmethod
    def _matches(
        value: StoredTraceAttestation,
        source: StoredTraceConsumption,
        previous: StoredTraceAttestation | None,
    ) -> None:
        if (
            value.consumer_id != source.before.configuration.consumer_id
            or value.configuration_fingerprint != source.before.configuration.fingerprint
            or value.revision != source.position.revision
            or value.first_seq != source.before.after_seq + 1
            or value.last_seq != source.position.after_seq
            or value.source_count != len(source.sources)
            or value.telemetry_count != sum(item.observation is not None for item in source.sources)
            or value.previous_id != (previous.request.id if previous else None)
            or value.previous_fingerprint != (previous.fingerprint if previous else None)
            or (previous is not None and previous.created_at > value.created_at)
        ):
            raise inconsistent_attestation()

    def reverify(self, id: UUID) -> StoredTraceAttestation:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.reverify_in_transaction(session, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify_in_transaction(self, session: Session, id: UUID) -> StoredTraceAttestation:
        """Verify this segment, its anchor and direct binding, not all prior sources."""
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                repo = TraceAttestationRepository(session)
                value = repo.get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Source attestation not found", 404)
                if value.created_at > self._now():
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Source attestation is not reached", 503
                    )
                self._matches(value, *self._source(session, value.request))
                if repo.get(id) != value:
                    raise inconsistent_attestation()
                self._deadline(deadline)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_attestation() from None

    def record(
        self, context: UsageTraceConsumeContext, request: TraceAttestationRequest
    ) -> TraceAttestationReceipt:
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise self._failure(error) from None

    def record_in_transaction(
        self, session: Session, context: UsageTraceConsumeContext, request: TraceAttestationRequest
    ) -> TraceAttestationReceipt:
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        request = TraceAttestationRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = TraceAttestationRepository(session)
                repo.lock("operation", str(request.id))
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request:
                        raise DomainError("CONFLICT", "Source attestation has different input", 409)
                    return TraceAttestationReceipt(attestation=prior, duplicate=True)
                if request.source_cutoff > self._now():
                    raise DomainError("VALIDATION_ERROR", "Source cutoff is in the future", 422)
                original = UsageTraceRepository(session).get(request.record_id)
                if original is None:
                    raise DomainError("NOT_FOUND", "Candidate consumption record not found", 404)
                repo.lock("consumer", original.before.configuration.consumer_id)
                if repo.get(request.record_id, by_record=True) is not None:
                    raise DomainError("CONFLICT", "Source segment already has an attestation", 409)
                source, previous = self._source(session, request)
                self._deadline(deadline)
                now, event_id = self._now(), uuid4()
                unchecked = StoredTraceAttestation.model_construct(
                    request=request,
                    event_id=event_id,
                    consumer_id=source.before.configuration.consumer_id,
                    configuration_fingerprint=source.before.configuration.fingerprint,
                    revision=source.position.revision,
                    first_seq=source.before.after_seq + 1,
                    last_seq=source.position.after_seq,
                    source_count=len(source.sources),
                    telemetry_count=sum(item.observation is not None for item in source.sources),
                    previous_id=previous.request.id if previous else None,
                    previous_fingerprint=previous.fingerprint if previous else None,
                    created_at=now,
                    fingerprint="0" * 64,
                )
                value = StoredTraceAttestation.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": attestation_fingerprint(unchecked),
                    }
                )
                self._append(repo, context, value)
                session.flush()
                if self.reverify_in_transaction(session, request.id) != value:
                    raise inconsistent_attestation()
                self._deadline(deadline)
                return TraceAttestationReceipt(attestation=value, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_attestation() from None

    @staticmethod
    def _append(
        repo: TraceAttestationRepository,
        context: UsageTraceConsumeContext,
        value: StoredTraceAttestation,
    ) -> None:
        session, event_id = repo.session, str(value.event_id)
        action = "lyra.usage.source_attested"
        payload = {
            "attestation_id": str(value.request.id),
            "record_id": str(value.request.record_id),
            "consumer_id": value.consumer_id,
            "revision": value.revision,
            "first_seq": value.first_seq,
            "last_seq": value.last_seq,
            "fingerprint": value.fingerprint,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="usage_trace_attestation",
                aggregate_id=value.consumer_id,
                aggregate_version=value.revision,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.record_id),
                payload=payload,
            )
        )
        session.flush()
        repo.append(value)
        session.add(OutboxEntry(event_id=event_id, created_at=value.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=str(value.request.id),
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
