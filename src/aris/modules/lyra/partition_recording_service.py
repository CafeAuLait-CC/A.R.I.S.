"""Atomically record a source-verified partition through existing ledger services."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_schemas import (
    HistoricalNodeUserBatch,
    HistoricalNodeUserBatchResult,
    HistoricalNodeUserQuery,
    HistoricalNodeUserResolution,
)
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
from aris.modules.lyra.partition_recording_schemas import (
    PartitionRecordingReceipt,
    PartitionRecordingRequest,
    StoredPartitionRecording,
    partition_recording_fingerprint,
)
from aris.modules.lyra.residency_attribution import HistoricalIdentityReader
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionReference,
    VerifiedResidencyPartition,
)
from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository


class PartitionEvidenceReader(Protocol):
    def verify_in_transaction(
        self, session: Session, request: ResidencyPartitionReference
    ) -> VerifiedResidencyPartition: ...


class PartitionRecordingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        evidence: PartitionEvidenceReader,
        identities: HistoricalIdentityReader,
        derivations: UsageDerivationService,
        ledger: UsageLedgerService,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("partition recording requires a bounded internal timeout")
        self.sessions, self.evidence, self.identities, self.derivations, self.ledger = (
            sessions,
            evidence,
            identities,
            derivations,
            ledger,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("partition recording clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Partition recording budget exceeded", 503
            )

    def _configuration(self, request: PartitionRecordingRequest) -> UsagePostingConfiguration:
        value = self.ledger.configuration
        if value is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Partition recording is not configured", 503)
        value = UsagePostingConfiguration.model_validate(value.model_dump(mode="json"))
        if value.fingerprint != request.expected_recording_configuration_fingerprint:
            raise DomainError("CONFLICT", "Partition recording configuration differs", 409)
        return value

    def _proof(
        self, session: Session, request: PartitionRecordingRequest
    ) -> VerifiedResidencyPartition:
        value = self.evidence.verify_in_transaction(session, request.partition)
        value = VerifiedResidencyPartition.model_validate(value.model_dump(mode="json"))
        if value.request != request.partition or value.read_at > self._now():
            raise DomainError("CONFLICT", "Partition evidence binding or clock differs", 409)
        return value

    def _identity(
        self,
        session: Session,
        request: PartitionRecordingRequest,
        proof: VerifiedResidencyPartition,
    ) -> HistoricalNodeUserResolution:
        run = proof.parent_run
        query = HistoricalNodeUserQuery(
            node_id=run.first.node_id,
            os_username=run.username,
            start_at=run.first.compute_observed_at,
            end_at=run.last.compute_observed_at,
            as_of=request.partition.reference.source_cutoff,
            through_revision=request.identity_revision,
        )
        result = self.identities.resolve_many_in_transaction(
            session, HistoricalNodeUserBatch(queries=(query,))
        )
        result = HistoricalNodeUserBatchResult.model_validate(result.model_dump(mode="json"))
        if (
            len(result.answers) != 1
            or result.answers[0].query != query
            or result.directory_revision != request.identity_revision
        ):
            raise DomainError("CONFLICT", "Parent residency identity snapshot differs", 409)
        identity = result.answers[0]
        if identity.status != "resolved":
            raise DomainError(
                "USAGE_NEEDS_REVIEW", "Entire parent residency identity is unresolved", 409
            )
        return identity

    def record(
        self, context: UsagePostingContext, request: PartitionRecordingRequest
    ) -> PartitionRecordingReceipt:
        with self.sessions() as session:
            try:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise DomainError(
                    database_failure(error), "Partition recording database is unavailable", 503
                ) from None

    def record_in_transaction(
        self, session: Session, context: UsagePostingContext, request: PartitionRecordingRequest
    ) -> PartitionRecordingReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = PartitionRecordingRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = ClosedRunRecordingRepository(session)
                repo.lock(request.id)
                prior = repo.get_partition(request.id)
                if prior is not None:
                    if prior.request != request or prior.actor != context.actor:
                        raise DomainError(
                            "CONFLICT", "Partition operation has different input", 409
                        )
                    return prior.receipt(duplicate=True)
                config = self._configuration(request)
                proof = self._proof(session, request)
                if proof.parent_run.rule_fingerprint != config.timing_fingerprint:
                    raise DomainError("CONFLICT", "Partition parent timing rules differ", 409)
                if proof.parent_run.first.compute_observed_at < config.recording_start_at:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Parent residency precedes the recording start", 409
                    )
                identity = self._identity(session, request, proof)
                derived = self.derivations.record_in_transaction(
                    session,
                    UsageDerivationContext(request_id=context.request_id),
                    UsageDerivationRequest(
                        id=request.derivation_id,
                        sources=proof.sources,
                        as_of=request.partition.reference.source_cutoff,
                        identity_revision=request.identity_revision,
                        continuity=proof.continuity,
                        formation=proof.formation,
                    ),
                )
                artifact = self.derivations.exact_in_transaction(session, derived.id).artifact
                attributed = artifact.analysis.request.attribution
                matches = [
                    index
                    for index, value in enumerate(attributed.residency.sessions)
                    if value == proof.candidate
                ]
                if len(matches) != 1:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW",
                        "Partition differs from the derived original window",
                        409,
                    )
                index = matches[0]
                if (
                    attributed.attributions[index].identity.status != "resolved"
                    or attributed.attributions[index].identity.user_id != identity.user_id
                ):
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW",
                        "Partition identity differs from its entire parent",
                        409,
                    )
                self._deadline(deadline)
                if self._configuration(request) != config:
                    raise DomainError("CONFLICT", "Partition configuration changed", 409)
                posted = self.ledger.post_in_transaction(
                    session,
                    context,
                    UsagePostingRequest(
                        id=request.id,
                        derivation_id=request.derivation_id,
                        session_index=index,
                        expected_result_fingerprint=derived.result_fingerprint,
                        expected_configuration_fingerprint=config.fingerprint,
                    ),
                )
                if posted.duplicate:
                    raise DomainError(
                        "CONFLICT", "Partition ledger operation already has another owner", 409
                    )
                unchecked = StoredPartitionRecording.model_construct(
                    request=request,
                    proof=proof,
                    parent_identity=identity,
                    derivation_result_fingerprint=derived.result_fingerprint,
                    session_index=index,
                    ledger=posted,
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                value = StoredPartitionRecording.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": partition_recording_fingerprint(unchecked),
                    }
                )
                self._emit(session, context, value)
                session.flush()
                repo.append(value)
                session.flush()
                current = self._proof(session, request)
                if (
                    current.fingerprint != proof.fingerprint
                    or self._identity(session, request, current) != identity
                ):
                    raise DomainError(
                        "CONFLICT", "Partition original source or parent identity changed", 409
                    )
                if self._configuration(request) != config:
                    raise DomainError(
                        "CONFLICT", "Partition configuration changed before commit", 409
                    )
                self._deadline(deadline)
                if repo.get_partition(request.id) != value:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Partition recording is unavailable", 503
                    )
                return value.receipt(duplicate=False)
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Partition recording database is unavailable", 503
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Partition recording evidence is inconsistent", 503
            ) from None

    @staticmethod
    def _emit(
        session: Session, context: UsagePostingContext, value: StoredPartitionRecording
    ) -> None:
        action = "lyra.usage.partition_recorded"
        event_id = str(value.event_id)
        payload = {
            "operation_id": str(value.request.id),
            "node_id": value.ledger.head.node_id,
            "run_id": value.proof.parent_run.id,
            "partition_index": value.request.partition.partition_index,
            "partition_count": value.proof.partition_count,
            "fingerprint": value.fingerprint,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="usage_run_recording",
                aggregate_id=str(value.request.id),
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.partition.reference.record_id),
                payload=payload,
            )
        )
        session.flush()
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
