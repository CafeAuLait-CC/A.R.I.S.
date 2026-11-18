"""Atomic internal orchestration; no daemon, public route or quota activation."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.derivation_schemas import UsageDerivationContext, UsageDerivationRequest
from aris.modules.lyra.derivation_service import UsageDerivationService
from aris.modules.lyra.ledger_schemas import (
    UsagePostingConfiguration,
    UsagePostingContext,
    UsagePostingRequest,
)
from aris.modules.lyra.ledger_service import UsageLedgerService
from aris.modules.lyra.residency_evidence_schemas import (
    ResidencyRunReference,
    VerifiedResidencyWindow,
)
from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository
from aris.modules.lyra.run_recording_schemas import (
    ClosedRunRecordingReceipt,
    ClosedRunRecordingRequest,
    StoredClosedRunRecording,
    run_recording_fingerprint,
)
from aris.modules.vega.usage_archive_schemas import UsageArchivePage, UsageArchiveQuery
from aris.modules.vega.usage_schemas import UsageObservationQuery


class ClosedRunEvidenceReader(Protocol):
    def verify_in_transaction(
        self, session: Session, reference: ResidencyRunReference
    ) -> VerifiedResidencyWindow: ...


class ClosedRunArchiveReader(Protocol):
    def page_in_transaction(
        self, session: Session, query: UsageArchiveQuery
    ) -> UsageArchivePage: ...


class ClosedRunRecordingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        evidence: ClosedRunEvidenceReader,
        archive: ClosedRunArchiveReader,
        derivations: UsageDerivationService,
        ledger: UsageLedgerService,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("closed-run recording requires a bounded internal job timeout")
        self.sessions, self.evidence, self.archive = sessions, evidence, archive
        self.derivations, self.ledger = derivations, ledger
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("closed-run recording clock requires a timezone")
        return value.astimezone(UTC)

    def _configuration(self, request: ClosedRunRecordingRequest) -> UsagePostingConfiguration:
        value = self.ledger.configuration
        if value is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Usage recording is not configured", 503)
        value = UsagePostingConfiguration.model_validate(value.model_dump(mode="json"))
        if value.fingerprint != request.expected_recording_configuration_fingerprint:
            raise DomainError("CONFLICT", "Closed-run recording configuration differs", 409)
        return value

    def _proof(
        self, session: Session, request: ClosedRunRecordingRequest
    ) -> VerifiedResidencyWindow:
        value = self.evidence.verify_in_transaction(session, request.reference)
        try:
            value = VerifiedResidencyWindow.model_validate(value.model_dump(mode="json"))
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Closed-run proof contract is invalid", 503
            ) from None
        if (
            value.reference != request.reference
            or value.configuration_fingerprint != request.expected_trace_configuration_fingerprint
        ):
            raise DomainError("CONFLICT", "Closed-run evidence binding differs", 409)
        if value.state_at_record != "closed" or value.candidate_state != "stable_candidate":
            raise DomainError("USAGE_NEEDS_REVIEW", "Usage requires a stable closed residency", 409)
        if value.read_at > self._now():
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Closed-run proof is not reached", 503)
        return value

    def _sources(
        self, session: Session, proof: VerifiedResidencyWindow, deadline: float
    ) -> tuple[UsageObservationQuery, ...]:
        run = proof.run
        after = run.first.seq - 1
        result = []
        while after < run.last.seq:
            self._deadline(deadline)
            query = UsageArchiveQuery(
                agent_id=run.first.agent_id,
                node_id=run.first.node_id,
                after_seq=after,
                through_seq=run.last.seq,
            )
            original = self.archive.page_in_transaction(session, query)
            try:
                page = UsageArchivePage.model_validate(original.model_dump(mode="json"))
            except (ValueError, TypeError, AttributeError, RecursionError):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Closed-run archive contract is invalid", 503
                ) from None
            if page.query != query or page.state == "gap":
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Closed-run sources are incomplete", 503
                )
            for item in page.items:
                ref = item.reference
                if max(ref.observed_at, ref.received_at) > proof.reference.source_cutoff:
                    raise DomainError("CONFLICT", "Closed-run source cutoff differs", 409)
                if item.observation is not None:
                    result.append(
                        UsageObservationQuery(
                            agent_id=ref.agent_id,
                            node_id=ref.node_id,
                            message_id=ref.message_id,
                            expected_seq=ref.seq,
                        )
                    )
            if len(result) > 1024:
                raise DomainError(
                    "USAGE_EVIDENCE_TOO_LARGE", "Closed run exceeds derivation budget", 503
                )
            after = page.next_after_seq
        return tuple(result)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Closed-run recording budget exceeded", 503
            )

    def record(
        self, context: UsagePostingContext, request: ClosedRunRecordingRequest
    ) -> ClosedRunRecordingReceipt:
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise DomainError(
                    database_failure(error), "Closed-run recording database is unavailable", 503
                ) from None

    def record_in_transaction(
        self,
        session: Session,
        context: UsagePostingContext,
        request: ClosedRunRecordingRequest,
    ) -> ClosedRunRecordingReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = ClosedRunRecordingRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        with transaction_savepoint(session), session.no_autoflush:
            repo = ClosedRunRecordingRepository(session)
            repo.lock(request.id)
            prior = repo.get(request.id)
            if prior is not None:
                if prior.request != request or prior.actor != context.actor:
                    raise DomainError("CONFLICT", "Closed-run operation has different input", 409)
                return prior.receipt(duplicate=True)
            config = self._configuration(request)
            proof = self._proof(session, request)
            if config.timing_fingerprint != proof.run.rule_fingerprint:
                raise DomainError("CONFLICT", "Closed-run timing configuration differs", 409)
            sources = self._sources(session, proof, deadline)
            derived_request = UsageDerivationRequest(
                id=request.derivation_id,
                sources=sources,
                as_of=request.reference.source_cutoff,
                identity_revision=request.identity_revision,
                continuity=proof.continuity,
                formation=proof.formation,
            )
            derived = self.derivations.record_in_transaction(
                session, UsageDerivationContext(request_id=context.request_id), derived_request
            )
            artifact = self.derivations.exact_in_transaction(session, derived.id).artifact
            sessions = artifact.analysis.request.attribution.residency.sessions
            run = proof.run
            matches = [
                index
                for index, value in enumerate(sessions)
                if (
                    value.gpu_uuid,
                    value.uid,
                    value.username,
                    value.model,
                    value.evidence[0].previous,
                    value.evidence[-1].current,
                    len(value.evidence),
                    value.observed_microseconds,
                    value.state,
                )
                == (
                    run.gpu_uuid,
                    run.uid,
                    run.username,
                    run.model,
                    run.first,
                    run.last,
                    run.intervals,
                    run.observed_microseconds,
                    "stable_candidate",
                )
            ]
            if len(matches) != 1:
                raise DomainError(
                    "USAGE_NEEDS_REVIEW", "Closed run differs from derived windows", 409
                )
            self._deadline(deadline)
            if self._configuration(request) != config:
                raise DomainError("CONFLICT", "Closed-run configuration changed", 409)
            posted = self.ledger.post_in_transaction(
                session,
                context,
                UsagePostingRequest(
                    id=request.id,
                    derivation_id=derived.id,
                    session_index=matches[0],
                    expected_result_fingerprint=derived.result_fingerprint,
                    expected_configuration_fingerprint=config.fingerprint,
                ),
            )
            if posted.duplicate:
                raise DomainError("CONFLICT", "Ledger operation already has another owner", 409)
            self._deadline(deadline)
            now, event_id = self._now(), uuid4()
            unchecked = StoredClosedRunRecording.model_construct(
                request=request,
                actor=context.actor,
                proof=proof,
                derivation_result_fingerprint=derived.result_fingerprint,
                session_index=matches[0],
                ledger=posted,
                event_id=event_id,
                created_at=now,
                fingerprint="0" * 64,
            )
            record = StoredClosedRunRecording.model_validate(
                {
                    **unchecked.model_dump(mode="json"),
                    "fingerprint": run_recording_fingerprint(unchecked),
                }
            )
            action = "lyra.usage.closed_run_recorded"
            payload = {
                "operation_id": str(request.id),
                "node_id": posted.head.node_id,
                "run_id": run.id,
                "fingerprint": record.fingerprint,
                "quota_usage_available": False,
            }
            session.add(
                DomainEvent(
                    event_id=str(event_id),
                    event_type=action,
                    event_version=1,
                    aggregate_type="usage_run_recording",
                    aggregate_id=str(request.id),
                    aggregate_version=1,
                    occurred_at=now,
                    recorded_at=now,
                    actor=context.actor,
                    correlation_id=context.request_id,
                    causation_id=str(request.reference.record_id),
                    payload=payload,
                )
            )
            session.flush()
            repo.append(record)
            session.add(OutboxEntry(event_id=str(event_id), created_at=now))
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
            session.flush()
            final = self._proof(session, request)
            if final.model_dump(exclude={"read_at"}) != proof.model_dump(exclude={"read_at"}):
                raise DomainError("CONFLICT", "Closed-run proof changed before recording", 409)
            if self._configuration(request) != config:
                raise DomainError("CONFLICT", "Closed-run configuration changed", 409)
            self._deadline(deadline)
            saved = repo.get(request.id)
            if saved != record:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Closed-run receipt was not stored", 503
                )
            return saved.receipt(duplicate=False)
