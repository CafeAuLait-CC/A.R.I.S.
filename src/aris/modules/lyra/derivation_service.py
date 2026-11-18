"""Internal evidence recorder; no HTTP adapter, daemon or quota activation."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.lyra.derivation_repo import UsageDerivationRepository
from aris.modules.lyra.derivation_schemas import (
    MAX_DERIVATION_INPUT_BYTES,
    MAX_DERIVATION_RESULT_BYTES,
    DerivationSource,
    StoredUsageDerivation,
    UsageDerivationArtifact,
    UsageDerivationContext,
    UsageDerivationReceipt,
    UsageDerivationRequest,
    canonical_derivation,
    derivation_fingerprint,
)
from aris.modules.lyra.models import UsageDerivation
from aris.modules.lyra.reservation_links import ReservationLinkRequest, ResidencyReservationService
from aris.modules.lyra.residency_attribution import ResidencyAttributionService
from aris.modules.vega.usage_schemas import UsageObservationBatchQuery, UsageObservationFact


class HistoricalObservationReader(Protocol):
    def observations_in_transaction(
        self, session: Session, query: UsageObservationBatchQuery
    ) -> tuple[UsageObservationFact | None, ...]: ...


class UsageDerivationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        observations: HistoricalObservationReader,
        attribution: ResidencyAttributionService,
        reservations: ResidencyReservationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.observations = sessions, observations
        self.attribution, self.reservations = attribution, reservations
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("usage derivation clock requires a timezone")
        return utc(value)

    def _compute(
        self, session: Session, request: UsageDerivationRequest
    ) -> UsageDerivationArtifact:
        try:
            return self._validated_compute(session, request)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Usage derivation source contract is invalid", 503
            ) from None

    def _validated_compute(
        self, session: Session, request: UsageDerivationRequest
    ) -> UsageDerivationArtifact:
        facts = []
        originals = self.observations.observations_in_transaction(
            session,
            UsageObservationBatchQuery(
                queries=request.sources, contiguous_source_cutoff=request.as_of
            ),
        )
        if len(originals) != len(request.sources):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Usage source batch length differs", 503)
        for query, original in zip(request.sources, originals, strict=True):
            if original is None:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "An original usage source is unavailable", 503
                )
            fact = UsageObservationFact.model_validate(original.model_dump(mode="json"))
            if (fact.agent_id, fact.node_id, fact.message_id, fact.seq) != (
                query.agent_id,
                query.node_id,
                query.message_id,
                query.expected_seq,
            ):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Usage source binding differs", 503)
            facts.append(fact)
        attributed = self.attribution.analyze_in_transaction(
            session,
            facts,
            continuity=request.continuity,
            formation=request.formation,
            as_of=request.as_of,
            through_revision=request.identity_revision,
        )
        linked = self.reservations.link_in_transaction(
            session, ReservationLinkRequest(attribution=attributed, references=request.references)
        )
        return UsageDerivationArtifact(
            request=request,
            analysis=linked,
            sources=tuple(
                DerivationSource(
                    query=query,
                    fingerprint=fact.source_fingerprint,
                    observed_at=fact.observed_at,
                    received_at=fact.received_at,
                    compute_observed_at=fact.compute_observed_at,
                )
                for query, fact in zip(request.sources, facts, strict=True)
            ),
        )

    def exact(self, record_id: UUID) -> StoredUsageDerivation:
        with self.sessions() as session, session.no_autoflush:
            return self.exact_in_transaction(session, record_id)

    def exact_in_transaction(self, session: Session, record_id: UUID) -> StoredUsageDerivation:
        result = UsageDerivationRepository(session).get(record_id)
        if result is None:
            raise DomainError("NOT_FOUND", "Usage derivation not found", 404)
        return result

    def verified_in_transaction(self, session: Session, record_id: UUID) -> StoredUsageDerivation:
        """Validate fixed evidence against its owning source services before consumption."""
        with session.no_autoflush:
            result = self.exact_in_transaction(session, record_id)
            if result.receipt.created_at > self._now():
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Usage evidence is not reached", 503)
            if self._compute(session, result.artifact.request) != result.artifact:
                raise DomainError("CONFLICT", "Usage evidence sources differ", 409)
            return result

    def record(
        self, context: UsageDerivationContext, request: UsageDerivationRequest
    ) -> UsageDerivationReceipt:
        with self.sessions() as session:
            receipt = self.record_in_transaction(session, context, request)
            commit_owned_transaction(session)
            return receipt

    def record_in_transaction(
        self, session: Session, context: UsageDerivationContext, request: UsageDerivationRequest
    ) -> UsageDerivationReceipt:
        context = UsageDerivationContext.model_validate(context.model_dump(mode="json"))
        request = UsageDerivationRequest.model_validate(request.model_dump(mode="json"))
        raw_input = canonical_derivation(request)
        if len(raw_input.encode()) > MAX_DERIVATION_INPUT_BYTES:
            raise DomainError("VALIDATION_ERROR", "Usage derivation input exceeds its budget", 422)
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = UsageDerivationRepository(session)
                repo.lock(request.id)
                existing = repo.get(request.id)
                input_hash = derivation_fingerprint(raw_input, result=False)
                if existing is not None:
                    if existing.receipt.request_fingerprint != input_hash:
                        raise DomainError(
                            "CONFLICT", "Usage derivation ID has different input", 409
                        )
                    return existing.receipt
                if request.as_of > self._now():
                    raise DomainError(
                        "VALIDATION_ERROR", "Usage derivation cutoff is in the future", 422
                    )
                artifact = self._compute(session, request)
                raw_result = canonical_derivation(artifact)
                if len(raw_result.encode()) > MAX_DERIVATION_RESULT_BYTES:
                    raise DomainError(
                        "VALIDATION_ERROR", "Usage derivation result exceeds its budget", 422
                    )
                if self._compute(session, request) != artifact:
                    raise DomainError(
                        "CONFLICT", "Usage derivation sources changed during evaluation", 409
                    )
                now = self._now()
                if now < request.as_of:
                    raise DomainError(
                        "CONFLICT", "Usage derivation clock moved before its cutoff", 409
                    )
                result_hash = derivation_fingerprint(raw_result, result=True)
                event_id = str(uuid4())
                first = request.sources[0]
                session.add(
                    DomainEvent(
                        event_id=event_id,
                        event_type="lyra.usage.derivation_recorded",
                        event_version=1,
                        aggregate_type="usage_derivation",
                        aggregate_id=str(request.id),
                        aggregate_version=1,
                        occurred_at=now,
                        recorded_at=now,
                        actor=context.actor,
                        correlation_id=context.request_id,
                        causation_id=str(request.id),
                        payload=dict(
                            record_id=str(request.id),
                            node_id=first.node_id,
                            source_count=len(request.sources),
                            request_fingerprint=input_hash,
                            result_fingerprint=result_hash,
                            candidate_only=True,
                        ),
                    )
                )
                session.flush()
                repo.append(
                    UsageDerivation(
                        id=str(request.id),
                        event_id=event_id,
                        agent_id=first.agent_id,
                        node_id=first.node_id,
                        source_count=len(request.sources),
                        identity_revision=request.identity_revision,
                        as_of=request.as_of,
                        created_at=now,
                        request_fingerprint=input_hash,
                        result_fingerprint=result_hash,
                        canonical_request=raw_input,
                        canonical_result=raw_result,
                    )
                )
                session.add(OutboxEntry(event_id=event_id, created_at=now))
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=context.actor,
                        action="lyra.usage.derivation_recorded",
                        target=str(request.id),
                        request_id=context.request_id,
                        recorded_at=now,
                        changes=dict(
                            candidate_only=True,
                            source_count=len(request.sources),
                            request_fingerprint=input_hash,
                            result_fingerprint=result_hash,
                        ),
                    )
                )
                session.flush()
                stored = repo.get(request.id, duplicate=False)
                if stored is None:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Usage derivation write is unavailable", 503
                    )
                return stored.receipt
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Usage derivation storage is unavailable", 503
            ) from None
