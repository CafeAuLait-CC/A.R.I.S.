"""Re-read fixed manifests and original archives before trusting a run summary."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_evidence_schemas import (
    ResidencyRunReference,
    ResidencyVerificationLimits,
    VerifiedResidencyWindow,
    residency_evidence_fingerprint,
)
from aris.modules.lyra.residency_state import (
    ClosedResidencyCandidate,
    ResidencyRun,
    advance_residency_formation,
    preview_open_residencies,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    ConsumedTraceSource,
    StoredTraceConsumption,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import TraceArchiveReader
from aris.modules.lyra.trace_state import advance_usage_trace, observation_stamp
from aris.modules.vega.usage_archive_schemas import UsageArchivePage, UsageArchiveQuery


@dataclass
class _ManifestProgress:
    records: int = 0
    records_fingerprint: str = trace_fingerprint("proof_records", "[]")
    sources_fingerprint: str = trace_fingerprint("proof_sources", "[]")


class ResidencyRunEvidenceService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        archive: TraceArchiveReader,
        *,
        limits: ResidencyVerificationLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.archive = sessions, archive
        self.limits = ResidencyVerificationLimits.model_validate(
            (limits or ResidencyVerificationLimits()).model_dump(mode="json")
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("residency verification clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Residency verification exceeded its read budget", 503
            )

    @staticmethod
    def _database_failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Residency evidence database is unavailable",
            503,
        )

    def verify(self, reference: ResidencyRunReference) -> VerifiedResidencyWindow:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.limits.timeout_ms)
                return self.verify_in_transaction(session, reference)
        except DBAPIError as error:
            raise self._database_failure(error) from None

    def _manifest(
        self,
        repo: UsageTraceRepository,
        original: StoredTraceConsumption,
        run: ResidencyRun,
        reference: ResidencyRunReference,
        progress: _ManifestProgress,
        deadline: float,
    ) -> Iterator[ConsumedTraceSource]:
        config = original.before.configuration
        through_seq = original.position.after_seq
        expected, after_revision = run.first.seq, 0
        previous: StoredTraceConsumption | None = None
        while expected <= through_seq:
            self._deadline(deadline)
            records = repo.source_records(
                config.consumer_id,
                through_revision=original.position.revision,
                first_seq=run.first.seq,
                last_seq=through_seq,
                after_revision=after_revision,
            )
            if not records:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Original residency manifest is incomplete", 503
                )
            for record in records:
                progress.records += 1
                if progress.records > self.limits.max_records:
                    raise DomainError(
                        "USAGE_EVIDENCE_TOO_LARGE",
                        "Residency manifest exceeds its record budget",
                        503,
                    )
                if (
                    record.before.configuration != config
                    or record.created_at > reference.source_cutoff
                    or (record.request.id == original.request.id and record != original)
                ):
                    raise DomainError("CONFLICT", "Residency manifest scope or cutoff differs", 409)
                if previous is None:
                    if record.before.after_seq >= run.first.seq:
                        raise DomainError(
                            "CONFLICT", "Residency manifest starts after its source", 409
                        )
                    if record.before.record_id is not None:
                        parent = repo.get(record.before.record_id)
                        if parent is None or parent.position != record.before:
                            raise DomainError(
                                "DEPENDENCY_UNAVAILABLE",
                                "Residency manifest parent is unavailable",
                                503,
                            )
                elif record.before != previous.position:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Residency manifest chain is incomplete", 503
                    )
                progress.records_fingerprint = trace_fingerprint(
                    "proof_record",
                    json.dumps(
                        {
                            "previous": progress.records_fingerprint,
                            "id": str(record.request.id),
                            "fingerprint": record.fingerprint,
                            "revision": record.position.revision,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for source in record.sources:
                    if source.reference.seq < run.first.seq:
                        continue
                    if source.reference.seq > through_seq:
                        return
                    if source.reference.seq != expected:
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE",
                            "Residency manifest sequence is incomplete",
                            503,
                        )
                    progress.sources_fingerprint = trace_fingerprint(
                        "proof_source",
                        progress.sources_fingerprint + "\n" + canonical_trace(source),
                    )
                    yield source
                    expected += 1
                previous, after_revision = record, record.position.revision
                if expected > through_seq:
                    return

    def verify_in_transaction(
        self, session: Session, reference: ResidencyRunReference
    ) -> VerifiedResidencyWindow:
        """The owner supplies transaction budgets; this method never commits or flushes."""
        reference = ResidencyRunReference.model_validate(reference.model_dump(mode="json"))
        deadline = monotonic() + self.limits.timeout_ms / 1000
        try:
            with session.no_autoflush:
                if reference.source_cutoff > self._now():
                    raise DomainError(
                        "VALIDATION_ERROR", "Residency source cutoff is in the future", 422
                    )
                repo = UsageTraceRepository(session)
                original = repo.get(reference.record_id)
                if original is None:
                    raise DomainError("NOT_FOUND", "Residency candidate record is unknown", 404)
                if original.fingerprint != reference.expected_record_fingerprint:
                    raise DomainError(
                        "CONFLICT", "Residency candidate record fingerprint differs", 409
                    )
                if original.created_at > reference.source_cutoff:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE",
                        "Residency candidate is not available at this cutoff",
                        503,
                    )
                candidates = [
                    item for item in original.closed_runs if item.run.id == reference.run_id
                ]
                state_at_record: Literal["open", "closed"] = "closed"
                if original.after_residency is not None:
                    opened = [
                        item
                        for item in preview_open_residencies(original.after_residency)
                        if item.run.id == reference.run_id
                    ]
                    if opened:
                        candidates.extend(opened)
                        state_at_record = "open"
                if len(candidates) != 1:
                    raise DomainError("NOT_FOUND", "Residency run is not in this fixed record", 404)
                candidate = candidates[0]
                run, config = candidate.run, original.before.configuration
                through_seq = original.position.after_seq
                count = through_seq - run.first.seq + 1
                if count > self.limits.max_source_messages:
                    raise DomainError(
                        "USAGE_EVIDENCE_TOO_LARGE",
                        "Residency window exceeds its source budget",
                        503,
                    )
                progress = _ManifestProgress()
                manifest = self._manifest(repo, original, run, reference, progress, deadline)
                trace, residency, anchor = None, None, None
                found: list[ClosedResidencyCandidate] = []
                after, telemetry, sources = run.first.seq - 1, 0, 0
                while after < through_seq:
                    self._deadline(deadline)
                    query = UsageArchiveQuery(
                        agent_id=config.agent_id,
                        node_id=config.node_id,
                        after_seq=after,
                        through_seq=through_seq,
                        limit=128,
                    )
                    page = self.archive.page_in_transaction(session, query)
                    page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
                    if page.query != query or page.state == "gap" or not page.items:
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE",
                            "Original residency archive range is incomplete",
                            503,
                        )
                    for item in page.items:
                        self._deadline(deadline)
                        saved = next(manifest, None)
                        stamp = (
                            observation_stamp(item.observation)
                            if item.observation is not None
                            else None
                        )
                        if (
                            saved is None
                            or saved.reference != item.reference
                            or saved.observation != stamp
                        ):
                            raise DomainError(
                                "CONFLICT",
                                "Original residency source differs from its saved manifest",
                                409,
                            )
                        if (
                            max(item.reference.observed_at, item.reference.received_at)
                            > reference.source_cutoff
                        ):
                            raise DomainError(
                                "CONFLICT", "Original residency source follows its cutoff", 409
                            )
                        sources += 1
                        fact = item.observation
                        if fact is None:
                            continue
                        telemetry += 1
                        step = advance_usage_trace(
                            trace, fact, rule=config.continuity, anchor=anchor
                        )
                        formed = advance_residency_formation(
                            residency,
                            step,
                            continuity=config.continuity,
                            formation=config.formation,
                        )
                        found.extend(item for item in formed.closed if item.run.id == run.id)
                        if len(found) > 1:
                            raise DomainError(
                                "CONFLICT", "Residency run repeats in original sources", 409
                            )
                        trace, residency = step.checkpoint, formed.checkpoint
                        if trace.anchor.reference.message_id == fact.message_id:
                            anchor = fact
                    after = page.next_after_seq
                if next(manifest, None) is not None or sources != count:
                    raise DomainError(
                        "CONFLICT", "Residency source range differs from its manifest", 409
                    )
                if state_at_record == "open":
                    if found:
                        raise DomainError(
                            "CONFLICT", "Original sources close this supposedly open run", 409
                        )
                    if residency is not None:
                        found.extend(
                            item
                            for item in preview_open_residencies(residency)
                            if item.run.id == run.id
                        )
                if len(found) != 1 or found[0] != candidate:
                    raise DomainError(
                        "CONFLICT", "Original sources do not reproduce this residency window", 409
                    )
                if repo.get(reference.record_id) != original:
                    raise DomainError(
                        "CONFLICT", "Residency candidate changed during verification", 409
                    )
                self._deadline(deadline)
                unchecked = VerifiedResidencyWindow.model_construct(
                    reference=reference,
                    consumer_id=config.consumer_id,
                    consumer_revision=original.position.revision,
                    configuration_fingerprint=config.fingerprint,
                    record_created_at=original.created_at,
                    run=run,
                    continuity=config.continuity,
                    formation=config.formation,
                    candidate_state=candidate.state,
                    state_at_record=state_at_record,
                    verified_through_seq=through_seq,
                    source_count=sources,
                    telemetry_count=telemetry,
                    manifest_records=progress.records,
                    manifest_fingerprint=progress.records_fingerprint,
                    sources_fingerprint=progress.sources_fingerprint,
                    read_at=self._now(),
                    fingerprint="0" * 64,
                )
                return VerifiedResidencyWindow.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": residency_evidence_fingerprint(unchecked),
                    }
                )
        except DBAPIError as error:
            raise self._database_failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Residency source evidence is inconsistent", 503
            ) from None
