"""Read sampled local segments through attested prefixes and original sources."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_segments import (
    ResidencySegmentAnalysis,
    ResidencySegmentQuery,
    rebuild_residency_segments,
    segment_analysis_fingerprint,
)
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_service import TraceArchiveReader, TraceObservationReader
from aris.modules.vega.usage_archive_schemas import UsageArchivePage, UsageArchiveQuery
from aris.modules.vega.usage_schemas import UsageObservationQuery


class SegmentAttestationReader(Protocol):
    def reverify_in_transaction(self, session: Session, id: UUID) -> StoredTraceAttestation: ...


class ResidencySegmentService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        attestations: SegmentAttestationReader,
        archive: TraceArchiveReader,
        observations: TraceObservationReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.attestations = sessions, attestations
        self.archive, self.observations = archive, observations
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("residency segment clock requires a timezone")
        return value.astimezone(UTC)

    def analyze(self, query: ResidencySegmentQuery) -> ResidencySegmentAnalysis:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 10000)
                return self.analyze_in_transaction(session, query)
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Residency segment database unavailable", 503
            ) from None

    def analyze_in_transaction(
        self, session: Session, query: ResidencySegmentQuery
    ) -> ResidencySegmentAnalysis:
        query = ResidencySegmentQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 10
        try:
            with session.no_autoflush:
                proof = self.attestations.reverify_in_transaction(session, query.attestation_id)
                proof = StoredTraceAttestation.model_validate(proof.model_dump(mode="json"))
                if (
                    proof.request.id != query.attestation_id
                    or proof.fingerprint != query.expected_attestation_fingerprint
                ):
                    raise DomainError(
                        "CONFLICT", "Residency segment attestation binding differs", 409
                    )
                repo = UsageTraceRepository(session)
                record = repo.get(proof.request.record_id)
                if (
                    record is None
                    or record.fingerprint != proof.request.expected_record_fingerprint
                ):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Attested trace record is unavailable", 503
                    )
                config = record.before.configuration
                source_query = UsageArchiveQuery(
                    agent_id=config.agent_id,
                    node_id=config.node_id,
                    after_seq=record.before.after_seq,
                    through_seq=record.position.after_seq,
                    limit=len(record.sources),
                )
                page = self.archive.page_in_transaction(session, source_query)
                page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
                if page.query != source_query or any(
                    max(item.reference.received_at, item.reference.observed_at)
                    > proof.request.source_cutoff
                    for item in page.items
                ):
                    raise DomainError(
                        "CONFLICT", "Segment archive query or source cutoff differs", 409
                    )
                anchor = None
                if record.before.trace is not None and any(
                    item.observation is not None for item in page.items
                ):
                    ref = record.before.trace.anchor.reference
                    anchor = self.observations.observation_in_transaction(
                        session,
                        UsageObservationQuery(
                            agent_id=ref.agent_id,
                            node_id=ref.node_id,
                            message_id=ref.message_id,
                            expected_seq=ref.seq,
                        ),
                    )
                    if (
                        anchor is not None
                        and max(anchor.received_at, anchor.observed_at)
                        > proof.request.source_cutoff
                    ):
                        raise DomainError(
                            "CONFLICT", "Segment anchor follows the source cutoff", 409
                        )
                segments = rebuild_residency_segments(record, page, anchor)
                if repo.get(record.request.id) != record:
                    raise DomainError(
                        "CONFLICT", "Attested trace changed during segment replay", 409
                    )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Residency segment budget exceeded", 503
                    )
                unchecked = ResidencySegmentAnalysis.model_construct(
                    query=query,
                    attestation=proof,
                    agent_id=config.agent_id,
                    node_id=config.node_id,
                    continuity=config.continuity,
                    formation=config.formation,
                    segments=segments,
                    read_at=self._now(),
                    fingerprint="0" * 64,
                )
                result = ResidencySegmentAnalysis.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": segment_analysis_fingerprint(unchecked),
                    }
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Residency segment budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Residency segment database unavailable", 503
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Residency segment evidence is inconsistent", 503
            ) from None
