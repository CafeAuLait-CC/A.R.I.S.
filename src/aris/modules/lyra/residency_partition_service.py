"""Verify bounded original windows; closed partitions retain their separate authority."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_observed_window_schemas import (
    ResidencyObservedWindowReference,
    VerifiedResidencyObservedWindow,
    observed_window_fingerprint,
)
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionLimits,
    ResidencyPartitionMethod,
    ResidencyPartitionReference,
    VerifiedResidencyPartition,
    partition_fingerprint,
)
from aris.modules.lyra.residency_segment_service import SegmentAttestationReader
from aris.modules.lyra.residency_sessions import ResidencySessionCandidate, build_residency_sessions
from aris.modules.lyra.residency_state import (
    ClosedResidencyCandidate,
    ResidencyRun,
    preview_open_residencies,
)
from aris.modules.lyra.segment_store_schemas import (
    ResidencySegmentLookup,
    ResidencySegmentPage,
    StoredResidencySegmentItem,
)
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_attestation_service import TraceRecordVerifier
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    ConsumedTraceSource,
    StoredTraceConsumption,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import TraceArchiveReader
from aris.modules.lyra.trace_state import observation_stamp
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_SOURCE_CHARACTERS,
    ArchivedUsageItem,
    UsageArchivePage,
    UsageArchiveQuery,
)
from aris.modules.vega.usage_schemas import UsageObservationQuery


@dataclass
class _Manifest:
    records: int = 0
    characters: int = 0
    fingerprint: str = trace_fingerprint("partition_manifest", "[]")


@dataclass(frozen=True)
class _Window:
    candidate: ResidencySessionCandidate
    sources: tuple[UsageObservationQuery, ...]
    first_seq: int
    last_seq: int
    source_count: int
    source_characters: int
    manifest_records: int
    index_fingerprint: str
    manifest_fingerprint: str
    sources_fingerprint: str


class ResidencyPartitionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        attestations: SegmentAttestationReader,
        records: TraceRecordVerifier,
        segments: ResidencySegmentStore,
        archive: TraceArchiveReader,
        *,
        method: ResidencyPartitionMethod,
        limits: ResidencyPartitionLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.attestations, self.records, self.segments, self.archive = (
            sessions,
            attestations,
            records,
            segments,
            archive,
        )
        self.method = ResidencyPartitionMethod.model_validate(method.model_dump(mode="json"))
        self.limits = ResidencyPartitionLimits.model_validate(
            (limits or ResidencyPartitionLimits()).model_dump(mode="json")
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("partition clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Residency partition read budget exceeded", 503
            )

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Residency partition database is unavailable",
            503,
        )

    def verify(self, request: ResidencyPartitionReference) -> VerifiedResidencyPartition:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.limits.timeout_ms)
                return self.verify_in_transaction(session, request)
        except DBAPIError as error:
            raise self._failure(error) from None

    def observed_window(
        self, request: ResidencyObservedWindowReference
    ) -> VerifiedResidencyObservedWindow:
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                transaction_budget(session, 100, 1000, self.limits.timeout_ms)
                return self.observed_window_in_transaction(session, request)
        except DBAPIError as error:
            raise self._failure(error) from None

    def observed_window_in_transaction(
        self, session: Session, request: ResidencyObservedWindowReference
    ) -> VerifiedResidencyObservedWindow:
        request = ResidencyObservedWindowReference.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.limits.timeout_ms / 1000
        try:
            with session.no_autoflush:
                if request.reference.source_cutoff > self._now():
                    raise DomainError(
                        "VALIDATION_ERROR", "Observed source cutoff is in the future", 422
                    )
                original = self._attested_record(
                    session,
                    request.reference,
                    request.attestation_id,
                    request.expected_attestation_fingerprint,
                    request.expected_trace_configuration_fingerprint,
                )
                matches: list[tuple[ClosedResidencyCandidate, Literal["open", "closed"]]] = [
                    (item, "closed")
                    for item in original.closed_runs
                    if item.run.id == request.reference.run_id
                ]
                if original.after_residency is not None:
                    matches.extend(
                        (item, "open")
                        for item in preview_open_residencies(original.after_residency)
                        if item.run.id == request.reference.run_id
                    )
                if len(matches) != 1:
                    raise DomainError("NOT_FOUND", "Run is not in this fixed trace record", 404)
                parent, state = matches[0]
                if request.through_interval > parent.run.intervals:
                    raise DomainError(
                        "NOT_FOUND", "Observed window extends beyond this prefix", 404
                    )
                window = self._window(
                    session,
                    original,
                    parent.run,
                    request.after_interval,
                    request.through_interval,
                    request,
                    deadline,
                )
                current = self._attested_record(
                    session,
                    request.reference,
                    request.attestation_id,
                    request.expected_attestation_fingerprint,
                    request.expected_trace_configuration_fingerprint,
                )
                if current != original:
                    raise DomainError(
                        "CONFLICT", "Observed parent changed during verification", 409
                    )
                self._deadline(deadline)
                config = original.before.configuration
                draft = VerifiedResidencyObservedWindow.model_construct(
                    request=request,
                    consumer_id=config.consumer_id,
                    consumer_revision=original.position.revision,
                    record_created_at=original.created_at,
                    state_at_record=state,
                    parent_run=parent.run,
                    parent_candidate_state=parent.state,
                    continuity=config.continuity,
                    formation=config.formation,
                    candidate=window.candidate,
                    sources=window.sources,
                    verified_first_seq=window.first_seq,
                    verified_last_seq=window.last_seq,
                    source_count=window.source_count,
                    source_characters=window.source_characters,
                    manifest_records=window.manifest_records,
                    index_fingerprint=window.index_fingerprint,
                    manifest_fingerprint=window.manifest_fingerprint,
                    sources_fingerprint=window.sources_fingerprint,
                    read_at=self._now(),
                    fingerprint="0" * 64,
                )
                result = VerifiedResidencyObservedWindow.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": observed_window_fingerprint(draft),
                    }
                )
                self._deadline(deadline)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Observed residency evidence is inconsistent", 503
            ) from None

    def _attested_record(
        self,
        session: Session,
        reference: ResidencyRunReference,
        attestation_id: UUID,
        expected_attestation_fingerprint: str,
        expected_configuration_fingerprint: str,
    ) -> StoredTraceConsumption:
        proof = self.attestations.reverify_in_transaction(session, attestation_id)
        proof = StoredTraceAttestation.model_validate(proof.model_dump(mode="json"))
        if (
            proof.request.id != attestation_id
            or proof.fingerprint != expected_attestation_fingerprint
            or proof.request.record_id != reference.record_id
            or proof.request.expected_record_fingerprint != reference.expected_record_fingerprint
            or proof.configuration_fingerprint != expected_configuration_fingerprint
        ):
            raise DomainError("CONFLICT", "Fixed run attestation binding differs", 409)
        record = self.records.verify_record_in_transaction(
            session,
            reference.record_id,
            reference.expected_record_fingerprint,
            reference.source_cutoff,
        )
        record = StoredTraceConsumption.model_validate(record.model_dump(mode="json"))
        if (
            record.request.id != reference.record_id
            or record.fingerprint != reference.expected_record_fingerprint
            or record.before.configuration.fingerprint != expected_configuration_fingerprint
            or proof.consumer_id != record.before.configuration.consumer_id
            or proof.revision != record.position.revision
            or proof.first_seq != record.before.after_seq + 1
            or proof.last_seq != record.position.after_seq
        ):
            raise DomainError("CONFLICT", "Fixed run trace binding differs", 409)
        return record

    def _closed(
        self, session: Session, request: ResidencyPartitionReference
    ) -> StoredTraceConsumption:
        return self._attested_record(
            session,
            request.reference,
            request.closing_attestation_id,
            request.expected_closing_attestation_fingerprint,
            request.expected_trace_configuration_fingerprint,
        )

    def _index(
        self,
        session: Session,
        record: StoredTraceConsumption,
        run_id: str,
        after: int,
        through: int,
        deadline: float,
    ) -> tuple[StoredResidencySegmentItem, ...]:
        result: list[StoredResidencySegmentItem] = []
        cursor = after
        while cursor < through:
            self._deadline(deadline)
            query = ResidencySegmentLookup(
                consumer_id=record.before.configuration.consumer_id,
                run_id=run_id,
                after_interval=cursor,
                through_interval=through,
            )
            page = self.segments.lookup_in_transaction(session, query)
            page = ResidencySegmentPage.model_validate(page.model_dump(mode="json"))
            if page.query != query or page.state == "gap" or not page.items:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Original residency interval index is incomplete", 503
                )
            if result and result[-1].segment.last != page.items[0].segment.first:
                raise DomainError("CONFLICT", "Indexed residency partitions do not adjoin", 409)
            result.extend(page.items)
            cursor = page.next_after_interval
            if len(result) > 130:
                raise DomainError(
                    "USAGE_EVIDENCE_TOO_LARGE", "Residency partition exceeds its index budget", 503
                )
        return tuple(result)

    def _manifest(
        self,
        repo: UsageTraceRepository,
        original: StoredTraceConsumption,
        first: int,
        last: int,
        request: ResidencyPartitionReference | ResidencyObservedWindowReference,
        progress: _Manifest,
        deadline: float,
    ) -> Iterator[ConsumedTraceSource]:
        config = original.before.configuration
        expected = first
        revision = 0
        previous = None
        while expected <= last:
            self._deadline(deadline)
            records = repo.source_records(
                config.consumer_id,
                through_revision=original.position.revision,
                first_seq=first,
                last_seq=last,
                after_revision=revision,
            )
            if not records:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Original partition manifest is incomplete", 503
                )
            for record in records:
                progress.records += 1
                progress.characters += len(canonical_trace(record))
                if (
                    progress.records > self.limits.max_manifest_records
                    or progress.characters > 4194304
                ):
                    raise DomainError(
                        "USAGE_EVIDENCE_TOO_LARGE",
                        "Residency partition manifest exceeds its budget",
                        503,
                    )
                if (
                    record.before.configuration != config
                    or record.created_at > request.reference.source_cutoff
                ):
                    raise DomainError("CONFLICT", "Partition manifest scope or cutoff differs", 409)
                if previous is not None and record.before != previous.position:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Partition manifest chain is incomplete", 503
                    )
                if previous is None and record.before.record_id is not None:
                    parent = repo.get(record.before.record_id)
                    if parent is None or parent.position != record.before:
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE",
                            "Partition manifest parent is unavailable",
                            503,
                        )
                progress.fingerprint = trace_fingerprint(
                    "partition_manifest_record",
                    progress.fingerprint
                    + "\n"
                    + str(record.request.id)
                    + "\n"
                    + record.fingerprint,
                )
                for source in record.sources:
                    if source.reference.seq < first:
                        continue
                    if source.reference.seq > last:
                        return
                    if source.reference.seq != expected:
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE",
                            "Partition manifest source sequence is incomplete",
                            503,
                        )
                    yield source
                    expected += 1
                    if expected > last:
                        return
                previous, revision = record, record.position.revision

    def _originals(
        self,
        session: Session,
        record: StoredTraceConsumption,
        items: tuple[StoredResidencySegmentItem, ...],
        request: ResidencyPartitionReference | ResidencyObservedWindowReference,
        progress: _Manifest,
        deadline: float,
    ) -> tuple[tuple[ArchivedUsageItem, ...], int, str]:
        first, last = items[0].segment.first.seq, items[-1].segment.last.seq
        if last - first + 1 > self.limits.max_source_messages:
            raise DomainError(
                "USAGE_EVIDENCE_TOO_LARGE", "Partition original span exceeds its budget", 503
            )
        manifest = self._manifest(
            UsageTraceRepository(session), record, first, last, request, progress, deadline
        )
        config = record.before.configuration
        after = first - 1
        characters = 0
        result = []
        fingerprint = trace_fingerprint("partition_sources", "[]")
        while after < last:
            self._deadline(deadline)
            remaining = self.limits.max_source_characters - characters
            if remaining <= 0:
                raise DomainError(
                    "USAGE_EVIDENCE_TOO_LARGE", "Partition source body budget is exhausted", 503
                )
            query = UsageArchiveQuery(
                agent_id=config.agent_id,
                node_id=config.node_id,
                after_seq=after,
                through_seq=last,
                source_character_budget=min(remaining, MAX_ARCHIVE_SOURCE_CHARACTERS),
            )
            page = self.archive.page_in_transaction(session, query)
            page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
            if page.query != query or page.state == "gap" or not page.items:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Original partition archive is incomplete", 503
                )
            characters += page.source_characters
            for item in page.items:
                saved = next(manifest, None)
                stamp = (
                    observation_stamp(item.observation) if item.observation is not None else None
                )
                if saved is None or saved.reference != item.reference or saved.observation != stamp:
                    raise DomainError(
                        "CONFLICT", "Original partition source differs from its fixed manifest", 409
                    )
                if (
                    max(item.reference.observed_at, item.reference.received_at)
                    > request.reference.source_cutoff
                ):
                    raise DomainError("CONFLICT", "Partition source follows its cutoff", 409)
                fingerprint = trace_fingerprint(
                    "partition_source", fingerprint + "\n" + canonical_trace(saved)
                )
                result.append(item)
            after = page.next_after_seq
        if next(manifest, None) is not None or len(result) != last - first + 1:
            raise DomainError("CONFLICT", "Partition original source extent differs", 409)
        return tuple(result), characters, fingerprint

    def _window(
        self,
        session: Session,
        original: StoredTraceConsumption,
        run: ResidencyRun,
        after: int,
        through: int,
        request: ResidencyPartitionReference | ResidencyObservedWindowReference,
        deadline: float,
    ) -> _Window:
        config = original.before.configuration
        indexed = self._index(session, original, run.id, after, through, deadline)
        if any(
            item.segment.run_at_segment_end.first != run.first
            or item.segment.run_at_segment_end.intervals > run.intervals
            or item.segment.last.seq > run.last.seq
            for item in indexed
        ):
            raise DomainError("CONFLICT", "Indexed residency extends outside its fixed parent", 409)
        progress = _Manifest()
        sources, characters, source_fp = self._originals(
            session, original, indexed, request, progress, deadline
        )
        facts = tuple(item.observation for item in sources if item.observation is not None)
        if not 2 <= len(facts) <= 1024:
            raise DomainError(
                "USAGE_EVIDENCE_TOO_LARGE",
                "Partition replay exceeds its telemetry budget",
                503,
            )
        analysis = build_residency_sessions(
            facts, continuity=config.continuity, formation=config.formation
        )
        first, last = indexed[0].segment.first, indexed[-1].segment.last
        candidates = [
            value
            for value in analysis.sessions
            if (
                value.gpu_uuid,
                value.uid,
                value.username,
                value.model,
                value.evidence[0].previous,
                value.evidence[-1].current,
            )
            == (run.gpu_uuid, run.uid, run.username, run.model, first, last)
        ]
        broad_after = indexed[0].segment.interval_offset
        broad_through = indexed[-1].segment.run_at_segment_end.intervals
        if len(candidates) != 1 or len(candidates[0].evidence) != broad_through - broad_after:
            raise DomainError(
                "CONFLICT", "Original sources do not reproduce indexed residency edges", 409
            )
        edges = candidates[0].evidence[after - broad_after : through - broad_after]
        if len(edges) != through - after:
            raise DomainError(
                "CONFLICT", "Partition window does not fit its indexed original edges", 409
            )
        duration = edges[-1].current.compute_observed_at - edges[0].previous.compute_observed_at
        micros = (duration.days * 86400 + duration.seconds) * 1000000 + duration.microseconds
        candidate = ResidencySessionCandidate(
            gpu_uuid=run.gpu_uuid,
            uid=run.uid,
            username=run.username,
            model=run.model,
            evidence=edges,
            observed_microseconds=micros,
            state="stable_candidate"
            if len(edges) >= config.formation.minimum_intervals
            and micros >= config.formation.minimum_duration_seconds * 1000000
            else "insufficient_evidence",
            end_boundary="trace_end",
            boundary_reason="TRACE_END",
        )
        selected = tuple(
            UsageObservationQuery(
                agent_id=item.reference.agent_id,
                node_id=item.reference.node_id,
                message_id=item.reference.message_id,
                expected_seq=item.reference.seq,
            )
            for item in sources
            if item.observation is not None
            and edges[0].previous.seq <= item.reference.seq <= edges[-1].current.seq
        )
        return _Window(
            candidate=candidate,
            sources=selected,
            first_seq=sources[0].reference.seq,
            last_seq=sources[-1].reference.seq,
            source_count=len(sources),
            source_characters=characters,
            manifest_records=progress.records,
            index_fingerprint=trace_fingerprint(
                "partition_index", chr(10).join(canonical_trace(item) for item in indexed)
            ),
            manifest_fingerprint=progress.fingerprint,
            sources_fingerprint=source_fp,
        )

    def verify_in_transaction(
        self, session: Session, request: ResidencyPartitionReference
    ) -> VerifiedResidencyPartition:
        request = ResidencyPartitionReference.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.limits.timeout_ms / 1000
        try:
            with session.no_autoflush:
                if request.expected_method_fingerprint != self.method.fingerprint:
                    raise DomainError("CONFLICT", "Residency partition method differs", 409)
                if request.reference.source_cutoff > self._now():
                    raise DomainError(
                        "VALIDATION_ERROR", "Partition source cutoff is in the future", 422
                    )
                original = self._closed(session, request)
                matches = [
                    value
                    for value in original.closed_runs
                    if value.run.id == request.reference.run_id
                ]
                if len(matches) != 1 or matches[0].state != "stable_candidate":
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Partition requires one stable closed parent run", 409
                    )
                run = matches[0].run
                config = original.before.configuration
                count = (run.intervals + self.method.max_intervals - 1) // self.method.max_intervals
                if request.partition_index >= count:
                    raise DomainError(
                        "NOT_FOUND", "Residency partition index is outside the run", 404
                    )
                if run.intervals // count < config.formation.minimum_intervals:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW",
                        "Partition method cannot retain the original formation threshold",
                        409,
                    )
                after = request.partition_index * run.intervals // count
                through = (request.partition_index + 1) * run.intervals // count
                window = self._window(session, original, run, after, through, request, deadline)
                if window.candidate.state != "stable_candidate":
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW",
                        "Partition is shorter than its original formation threshold",
                        409,
                    )
                if self._closed(session, request) != original:
                    raise DomainError(
                        "CONFLICT", "Closed parent changed during partition verification", 409
                    )
                self._deadline(deadline)
                unchecked = VerifiedResidencyPartition.model_construct(
                    request=request,
                    consumer_id=config.consumer_id,
                    parent_run=run,
                    continuity=config.continuity,
                    formation=config.formation,
                    method=self.method,
                    partition_count=count,
                    after_interval=after,
                    through_interval=through,
                    candidate=window.candidate,
                    sources=window.sources,
                    verified_first_seq=window.first_seq,
                    verified_last_seq=window.last_seq,
                    source_count=window.source_count,
                    source_characters=window.source_characters,
                    manifest_records=window.manifest_records,
                    index_fingerprint=window.index_fingerprint,
                    manifest_fingerprint=window.manifest_fingerprint,
                    sources_fingerprint=window.sources_fingerprint,
                    read_at=self._now(),
                    fingerprint="0" * 64,
                )
                result = VerifiedResidencyPartition.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": partition_fingerprint(unchecked),
                    }
                )
                self._deadline(deadline)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Residency partition source evidence is inconsistent", 503
            ) from None
