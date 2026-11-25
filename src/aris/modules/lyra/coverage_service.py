"""Opt-in immutable per-source processing coverage, without enabling quota decisions."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_service import NodeUserHistoryService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.coverage_build_schemas import StoredCoverageBuild
from aris.modules.lyra.coverage_inputs import (
    coverage_parents,
    coverage_recording_candidates,
    find_coverage_recordings,
    get_coverage_recording,
    resolve_coverage_identities,
)
from aris.modules.lyra.coverage_ledger import CoverageLedgerEntry
from aris.modules.lyra.coverage_ledger_service import CoverageLedgerReader
from aris.modules.lyra.coverage_replay import CoverageReplay, replay_coverage
from aris.modules.lyra.coverage_repo import ProcessingCoverageRepository
from aris.modules.lyra.coverage_schemas import (
    ProcessingCoverageAnalysis,
    ProcessingCoverageConfiguration,
    ProcessingCoverageReceipt,
    ProcessingCoverageRequest,
    StoredProcessingCoverage,
    coverage_manifest_fingerprint,
)
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.processing_coverage import (
    CoverageRecording,
    processing_coverage,
    recording_id,
    recording_reference,
)
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from aris.modules.lyra.trace_consumer_service import TraceArchiveReader, TraceObservationReader
from aris.modules.vega.usage_archive_schemas import UsageArchiveQuery
from aris.modules.vega.usage_schemas import UsageObservationQuery


class ProcessingCoverageService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        attestations: TraceAttestationService,
        archive: TraceArchiveReader,
        observations: TraceObservationReader,
        identities: NodeUserHistoryService,
        *,
        configuration: ProcessingCoverageConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 10000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise ValueError("processing coverage requires a bounded internal timeout")
        self.sessions, self.attestations = sessions, attestations
        self.archive, self.observations, self.identities = archive, observations, identities
        self.configuration, self.timeout_ms = configuration, timeout_ms
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("processing coverage clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Processing coverage database is unavailable",
            503,
        )

    def _replay(
        self,
        session: Session,
        request: ProcessingCoverageRequest,
        config: ProcessingCoverageConfiguration,
    ) -> tuple[StoredTraceAttestation, CoverageReplay]:
        attested = self.attestations.reverify_in_transaction(session, request.attestation_id)
        if (
            attested.fingerprint != request.expected_attestation_fingerprint
            or attested.configuration_fingerprint != config.trace.fingerprint
        ):
            raise DomainError("CONFLICT", "Coverage attestation binding differs", 409)
        record = UsageTraceRepository(session).get(attested.request.record_id)
        if record is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Coverage source record is missing", 503)
        page = self.archive.page_in_transaction(
            session,
            UsageArchiveQuery(
                agent_id=config.trace.agent_id,
                node_id=config.trace.node_id,
                after_seq=record.before.after_seq,
                through_seq=record.position.after_seq,
                limit=len(record.sources),
            ),
        )
        anchor = None
        if record.before.trace is not None and any(value.observation for value in page.items):
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
        return attested, replay_coverage(record, page, anchor, config)

    def _analysis(
        self,
        session: Session,
        request: ProcessingCoverageRequest,
        config: ProcessingCoverageConfiguration,
        read_at: datetime,
        previous: ProcessingCoverageAnalysis | None = None,
        *,
        identity_revision: int | None = None,
        recording_revision: int | None = None,
    ) -> ProcessingCoverageAnalysis:
        attested, replay = self._replay(session, request, config)
        indexed = config.method == "indexed_sampled_residency_edges"
        ledger_context = None
        view = None
        reader = None
        deadline = monotonic() + self.timeout_ms / 1000
        if indexed:
            reader = CoverageLedgerReader(
                self.sessions, config.trace.node_id, clock=self.clock, timeout_ms=self.timeout_ms
            )
            ledger_context, view = reader.local(
                session, request, config, replay, read_at, deadline=deadline
            )
        entries = () if indexed else UsageLedgerRepository(session).prefix(request.ledger)
        recordings: tuple[CoverageRecording, ...]
        candidates: tuple[tuple[CoverageLedgerEntry, CoverageRecording], ...] = ()
        if previous is None and ledger_context is not None:
            candidates = coverage_recording_candidates(
                session,
                replay,
                request.start_at,
                request.end_at,
                request.ledger,
                ledger_context.active,
                config,
                read_at,
                through_recording_revision=recording_revision,
            )
            unique = {str(recording_id(value)): value for _, value in candidates}
            recordings = tuple(unique[key] for key in sorted(unique))
        elif previous is None:
            recordings = find_coverage_recordings(
                session,
                replay,
                request.start_at,
                request.end_at,
                request.ledger,
                entries,
                config,
                read_at,
                through_recording_revision=recording_revision,
            )
        else:
            found: list[CoverageRecording] = []
            for reference in previous.recordings:
                value = get_coverage_recording(session, reference.id)
                if (
                    value is None
                    or value.fingerprint != reference.fingerprint
                    or value.created_at > read_at
                ):
                    raise DomainError("CONFLICT", "Coverage closing receipt changed", 409)
                found.append(value)
            recordings = tuple(found)
        if reader is not None and ledger_context is not None:
            ledger_context = reader.dependencies(
                session,
                ledger_context,
                view,
                tuple(value.ledger.head for value in recordings),
                deadline=deadline,
            )
            for entry, value in candidates:
                if recording_reference(value, entry, config, ledger_context) is None:
                    raise DomainError("CONFLICT", "Stored closing receipt binding differs", 409)
        runs = coverage_parents(
            replay,
            request.start_at,
            request.end_at,
            recordings,
            entries,
            config,
            ledger_context=ledger_context,
        )
        identities = resolve_coverage_identities(
            session,
            self.identities,
            runs,
            read_at,
            previous=previous.identities if previous is not None else None,
            through_revision=identity_revision,
        )
        return processing_coverage(
            request,
            config,
            attested,
            replay,
            entries,
            recordings,
            identities,
            read_at=read_at,
            ledger_context=ledger_context,
        )

    def record_build_step_in_transaction(
        self,
        session: Session,
        context: UsageTraceConsumeContext,
        plan: StoredCoverageBuild,
        proof: StoredTraceAttestation,
    ) -> ProcessingCoverageReceipt:
        """Use the committed plan and proof the build service checked in this transaction."""
        revision = proof.revision
        if (
            not plan.start_revision < revision <= plan.target_revision
            or proof.consumer_id != plan.configuration.trace.consumer_id
            or proof.configuration_fingerprint != plan.configuration.trace.fingerprint
        ):
            raise DomainError("CONFLICT", "Coverage build source revision is absent", 409)
        request = ProcessingCoverageRequest(
            id=plan.manifest_id(revision),
            attestation_id=proof.request.id,
            expected_attestation_fingerprint=proof.fingerprint,
            expected_configuration_fingerprint=plan.configuration.fingerprint,
            start_at=plan.request.query.start_at,
            end_at=plan.request.query.end_at,
            ledger=plan.request.ledger,
        )
        repo = ProcessingCoverageRepository(session)
        repo.lock(request.id)
        existing = repo.get(request.id)
        if existing is not None:
            if (
                existing.analysis.request != request
                or existing.analysis.configuration != plan.configuration
                or existing.analysis.read_at != plan.read_at
                or (
                    existing.analysis.identities
                    and existing.analysis.identity_revision != plan.identity_revision
                )
            ):
                raise DomainError("CONFLICT", "Build manifest has different fixed input", 409)
            return ProcessingCoverageReceipt(manifest=existing, duplicate=True)
        if self.configuration != plan.configuration:
            raise DomainError("USAGE_NOT_CONFIGURED", "Coverage build configuration is paused", 503)
        analysis = self._analysis(
            session,
            request,
            plan.configuration,
            plan.read_at,
            identity_revision=plan.identity_revision,
            recording_revision=plan.recording_revision,
        )
        return ProcessingCoverageReceipt(
            manifest=self._save_analysis(repo, context, analysis),
            duplicate=False,
        )

    def exact(self, id: UUID) -> StoredProcessingCoverage:
        """Historical captured result; no claim that it describes today's processing state."""
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                result = ProcessingCoverageRepository(session).get(id)
                if result is None:
                    raise DomainError("NOT_FOUND", "Processing coverage not found", 404)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify(self, id: UUID) -> StoredProcessingCoverage:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.reverify_in_transaction(session, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify_in_transaction(self, session: Session, id: UUID) -> StoredProcessingCoverage:
        deadline = monotonic() + self.timeout_ms / 1000
        with session.no_autoflush:
            result = ProcessingCoverageRepository(session).get(id)
            if result is None:
                raise DomainError("NOT_FOUND", "Processing coverage not found", 404)
            analysis = result.analysis
            if (
                self._analysis(
                    session, analysis.request, analysis.configuration, analysis.read_at, analysis
                )
                != analysis
            ):
                raise DomainError("CONFLICT", "Captured processing coverage changed", 409)
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Coverage read budget exceeded", 503)
            return result

    def record(
        self,
        context: UsageTraceConsumeContext,
        request: ProcessingCoverageRequest,
    ) -> ProcessingCoverageReceipt:
        if self.configuration is None:
            raise DomainError("ACTION_NOT_CONFIGURED", "Processing coverage is not enabled", 503)
        with self.sessions() as session:
            try:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise self._failure(error) from None

    def record_in_transaction(
        self,
        session: Session,
        context: UsageTraceConsumeContext,
        request: ProcessingCoverageRequest,
    ) -> ProcessingCoverageReceipt:
        if self.configuration is None:
            raise DomainError("ACTION_NOT_CONFIGURED", "Processing coverage is not enabled", 503)
        config = ProcessingCoverageConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        request = ProcessingCoverageRequest.model_validate(request.model_dump(mode="json"))
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        with transaction_savepoint(session), session.no_autoflush:
            repo = ProcessingCoverageRepository(session)
            repo.lock(request.id)
            existing = repo.get(request.id)
            if existing is not None:
                if (
                    existing.analysis.request != request
                    or existing.analysis.configuration != config
                ):
                    raise DomainError("CONFLICT", "Coverage operation has different input", 409)
                return ProcessingCoverageReceipt(manifest=existing, duplicate=True)
            now = self._now()
            if request.end_at > now:
                raise DomainError("VALIDATION_ERROR", "Coverage window has not ended", 422)
            if (
                request.expected_configuration_fingerprint != config.fingerprint
                or request.ledger.node_id != config.trace.node_id
            ):
                raise DomainError("CONFLICT", "Coverage configuration differs", 409)
            ledger = UsageLedgerRepository(session)
            ledger.lock("node", config.trace.node_id)
            if ledger.head(config.trace.node_id) != request.ledger:
                raise DomainError(
                    "CONFLICT", "Capture requires the current committed ledger head", 409
                )
            analysis = self._analysis(session, request, config, now)
            # Capture detects new identity revisions and receipts; historical reads stay frozen.
            if self._analysis(session, request, config, now) != analysis:
                raise DomainError("CONFLICT", "Coverage inputs changed during capture", 409)
            if ledger.head(config.trace.node_id) != request.ledger:
                raise DomainError("CONFLICT", "Coverage ledger advanced during capture", 409)
            result = self._save_analysis(repo, context, analysis)
            if monotonic() >= deadline:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Coverage capture budget exceeded", 503
                )
            return ProcessingCoverageReceipt(manifest=result, duplicate=False)

    def _save_analysis(
        self,
        repo: ProcessingCoverageRepository,
        context: UsageTraceConsumeContext,
        analysis: ProcessingCoverageAnalysis,
    ) -> StoredProcessingCoverage:
        draft = StoredProcessingCoverage.model_construct(
            analysis=analysis,
            event_id=uuid4(),
            created_at=self._now(),
            fingerprint="0" * 64,
        )
        result = StoredProcessingCoverage.model_validate(
            {
                **draft.model_dump(mode="json"),
                "fingerprint": coverage_manifest_fingerprint(draft),
            }
        )
        self._append(repo, context, result)
        repo.session.flush()
        return result

    @staticmethod
    def _append(
        repo: ProcessingCoverageRepository,
        context: UsageTraceConsumeContext,
        value: StoredProcessingCoverage,
    ) -> None:
        analysis, session = value.analysis, repo.session
        action = "lyra.usage.processing_coverage_captured"
        payload = {
            "manifest_id": str(analysis.request.id),
            "node_id": analysis.request.ledger.node_id,
            "fingerprint": value.fingerprint,
            "ledger_revision": analysis.request.ledger.revision,
            "complete_within_source_window": analysis.complete_within_source_window,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=str(value.event_id),
                event_type=action,
                event_version=1,
                aggregate_type="usage_processing_coverage",
                aggregate_id=str(analysis.request.id),
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(analysis.request.attestation_id),
                payload=payload,
            )
        )
        session.flush()
        repo.append(value)
        session.add(OutboxEntry(event_id=str(value.event_id), created_at=value.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=str(analysis.request.id),
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
