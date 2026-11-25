"""Explicit current admission, one atomic source step, then a fixed historical window."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.coverage_build_repo import CoverageBuildRepository, inconsistent_build
from aris.modules.lyra.coverage_build_schemas import (
    AdmitCoverageWindow,
    AdvanceCoverageBuild,
    CoverageBuildProgress,
    CreateCoverageBuild,
    StoredCoverageBuild,
    StoredCoverageBuildStep,
    coverage_build_fingerprint,
    coverage_build_step_fingerprint,
)
from aris.modules.lyra.coverage_repo import ProcessingCoverageRepository
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.recording_order_repo import RecordingReceiptOrderRepository
from aris.modules.lyra.source_prefix_repo import SourcePrefixRepository
from aris.modules.lyra.trace_attestation_repo import TraceAttestationRepository
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.lyra.window_snapshot_schemas import (
    StoredUsageWindow,
    UsageWindowReceipt,
    WindowManifestSelection,
)
from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
from aris.modules.vega.archive_guard_service import protect_archive_sources_in_transaction
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


class CoverageBuildService:
    def __init__(
        self,
        coverage: ProcessingCoverageService,
        windows: UsageWindowSnapshotService,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if windows.coverage is not coverage or windows.sessions is not coverage.sessions:
            raise ValueError("coverage builds require one composed transaction boundary")
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 300000:
            raise ValueError("coverage builds require a bounded transaction timeout")
        self.coverage, self.windows, self.sessions = coverage, windows, coverage.sessions
        self.clock, self.timeout_ms = clock or (lambda: datetime.now(UTC)), timeout_ms

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coverage build clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Coverage build budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        if getattr(error.orig, "sqlstate", None) == "23505":
            code = "RETRYABLE_DATABASE_ERROR"
        return DomainError(code, "Coverage build database is unavailable", 503)

    def _begin(self, session: Session, *, snapshot: bool = False) -> None:
        if snapshot and session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        transaction_budget(session, 100, 1000, self.timeout_ms)

    def _active(
        self, expected: ProcessingCoverageConfiguration | None = None
    ) -> ProcessingCoverageConfiguration:
        config = self.coverage.configuration
        if config is None or self.windows.configuration != config:
            raise DomainError("USAGE_NOT_CONFIGURED", "Coverage build is paused", 503)
        config = ProcessingCoverageConfiguration.model_validate(config.model_dump(mode="json"))
        if expected is not None and config != expected:
            raise DomainError("CONFLICT", "Coverage build configuration changed", 409)
        return config

    def exact(self, id: UUID) -> StoredCoverageBuild:
        try:
            with self.sessions() as db:
                self._begin(db)
                plan = CoverageBuildRepository(db).get(id)
                if plan is None:
                    raise DomainError("NOT_FOUND", "Coverage build not found", 404)
                return plan
        except DBAPIError as error:
            raise self._failure(error) from None

    def admit_current(
        self,
        context: UsageTraceConsumeContext,
        target: AdmitCoverageWindow,
    ) -> StoredCoverageBuild:
        """Prepare today's boundaries; create rechecks them atomically before first admission."""
        config = self._active()
        target = AdmitCoverageWindow.model_validate(target.model_dump(mode="json"))
        if target.expected_configuration_fingerprint != config.fingerprint:
            raise DomainError("CONFLICT", "Coverage target configuration differs", 409)
        try:
            with self.sessions() as db:
                self._begin(db, snapshot=True)
                prior = CoverageBuildRepository(db).get(target.id)
                if prior is not None:
                    if (
                        prior.request.query != target.query
                        or prior.configuration != config
                        or prior.request.prefix_checkpoint != target.prefix_checkpoint
                    ):
                        raise DomainError("CONFLICT", "Coverage target has different input", 409)
                    return prior
                protect_archive_sources_in_transaction(
                    db,
                    UsageArchiveHeadQuery(
                        agent_id=config.trace.agent_id, node_id=config.trace.node_id
                    ),
                )
                source = self.windows.archive.head_in_transaction(
                    db,
                    UsageArchiveHeadQuery(
                        agent_id=config.trace.agent_id,
                        node_id=config.trace.node_id,
                    ),
                )
                head = UsageLedgerRepository(db).head(config.trace.node_id)
                request = CreateCoverageBuild(
                    id=target.id,
                    prefix_checkpoint=target.prefix_checkpoint,
                    query=target.query,
                    expected_configuration_fingerprint=config.fingerprint,
                    expected_source_fingerprint=source.fingerprint,
                    ledger=head,
                )
            return self.create(context, request)
        except DBAPIError as error:
            raise self._failure(error) from None

    def create(
        self, context: UsageTraceConsumeContext, request: CreateCoverageBuild
    ) -> StoredCoverageBuild:
        config = self._active()
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        request = CreateCoverageBuild.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                self._begin(db, snapshot=True)
                repo = CoverageBuildRepository(db)
                repo.lock(request.id)
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request or prior.configuration != config:
                        raise DomainError("CONFLICT", "Coverage build has different input", 409)
                    return prior
                if (
                    request.expected_configuration_fingerprint != config.fingerprint
                    or request.query.node_id != config.trace.node_id
                    or request.ledger.node_id != config.trace.node_id
                ):
                    raise DomainError("CONFLICT", "Coverage build configuration differs", 409)
                protect_archive_sources_in_transaction(
                    db,
                    UsageArchiveHeadQuery(
                        agent_id=config.trace.agent_id, node_id=config.trace.node_id
                    ),
                )
                source = self.windows.archive.head_in_transaction(
                    db,
                    UsageArchiveHeadQuery(
                        agent_id=config.trace.agent_id,
                        node_id=config.trace.node_id,
                    ),
                )
                if (
                    source.fingerprint != request.expected_source_fingerprint
                    or UsageLedgerRepository(db).head(config.trace.node_id) != request.ledger
                ):
                    raise DomainError("CONFLICT", "Coverage build current boundaries changed", 409)
                position = UsageTraceRepository(db).position(config.trace)
                if position.after_seq != source.highest_committed_seq:
                    raise DomainError(
                        "COVERAGE_SOURCE_NOT_CAUGHT_UP", "Coverage source has not caught up", 409
                    )
                tail = (
                    TraceAttestationRepository(db).get(position.record_id, by_record=True)
                    if position.record_id is not None
                    else None
                )
                if (tail is None) != (position.revision == 0) or (
                    tail is not None and tail.revision != position.revision
                ):
                    raise DomainError("CONFLICT", "Coverage source tail proof differs", 409)
                if request.prefix_checkpoint is not None:
                    prefix = SourcePrefixRepository(db).checked(
                        request.prefix_checkpoint,
                        config.trace,
                        request.query.start_at,
                    )
                    if prefix.proofs[-1].revision > position.revision:
                        raise DomainError(
                            "CONFLICT", "Source prefix exceeds the admitted source", 409
                        )
                revision = self.coverage.identities.revision_in_transaction(db)
                if revision is None:
                    raise DomainError(
                        "IDENTITY_UNINITIALIZED",
                        "Coverage identity history is not initialized",
                        409,
                    )
                now = self._now()
                if request.query.end_at > now:
                    raise DomainError("VALIDATION_ERROR", "Coverage window has not ended", 422)
                draft = StoredCoverageBuild.model_construct(
                    request=request,
                    configuration=config,
                    source=source,
                    tail=tail,
                    identity_revision=revision,
                    recording_revision=RecordingReceiptOrderRepository(db).head(
                        config.trace.node_id
                    ),
                    read_at=now,
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                plan = StoredCoverageBuild.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": coverage_build_fingerprint(draft),
                    }
                )
                self._active(config)
                self._event(
                    db,
                    context,
                    plan.event_id,
                    "admitted",
                    request.id,
                    plan.created_at,
                    plan.fingerprint,
                )
                repo.append(plan)
                db.flush()
                self._deadline(deadline)
                commit_owned_transaction(db)
                return plan
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _saved_step(
        db: Session, plan: StoredCoverageBuild, step: StoredCoverageBuildStep
    ) -> StoredCoverageBuildStep:
        manifest = ProcessingCoverageRepository(db).get(step.manifest.id)
        if (
            step.plan_id != plan.request.id
            or step.plan_fingerprint != plan.fingerprint
            or not plan.start_revision < step.revision <= plan.target_revision
            or step.manifest.id != plan.manifest_id(step.revision)
            or manifest is None
            or manifest.fingerprint != step.manifest.fingerprint
            or manifest.analysis.request.attestation_id != step.attestation_id
            or manifest.analysis.request.expected_attestation_fingerprint
            != step.attestation_fingerprint
            or manifest.analysis.request.ledger != plan.request.ledger
            or manifest.analysis.configuration != plan.configuration
            or manifest.analysis.read_at != plan.read_at
        ):
            raise inconsistent_build()
        return step

    def advance(
        self, context: UsageTraceConsumeContext, request: AdvanceCoverageBuild
    ) -> StoredCoverageBuildStep:
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        request = AdvanceCoverageBuild.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                self._begin(db)
                repo = CoverageBuildRepository(db)
                repo.lock(request.id)
                plan = repo.get(request.id)
                if plan is None:
                    raise DomainError("NOT_FOUND", "Coverage build not found", 404)
                if plan.fingerprint != request.expected_plan_fingerprint:
                    raise DomainError("CONFLICT", "Coverage plan fingerprint differs", 409)
                next_revision = request.expected_revision + 1
                existing = repo.step(request.id, next_revision)
                if existing is not None:
                    return self._saved_step(db, plan, existing)
                self._active(plan.configuration)
                previous = repo.latest(request.id)
                if (
                    previous.revision if previous else plan.start_revision
                ) != request.expected_revision:
                    raise DomainError("CONFLICT", "Coverage build progress changed", 409)
                if next_revision > plan.target_revision:
                    raise DomainError("COVERAGE_BUILD_FINISHED", "All source proofs are built", 409)
                proof = TraceAttestationRepository(db).at_revision(
                    plan.configuration.trace.consumer_id,
                    next_revision,
                )
                if proof is None:
                    raise DomainError("CONFLICT", "Coverage next source proof is missing", 409)
                after = (
                    previous.last_seq
                    if previous
                    else (
                        plan.request.prefix_checkpoint.last_seq
                        if plan.request.prefix_checkpoint
                        else plan.configuration.trace.initial_after_seq
                    )
                )
                if (
                    proof.first_seq != after + 1
                    or proof.previous_id
                    != (
                        previous.attestation_id
                        if previous
                        else (
                            plan.request.prefix_checkpoint.attestation_id
                            if plan.request.prefix_checkpoint
                            else None
                        )
                    )
                    or proof.previous_fingerprint
                    != (
                        previous.attestation_fingerprint
                        if previous
                        else (
                            plan.request.prefix_checkpoint.attestation_fingerprint
                            if plan.request.prefix_checkpoint
                            else None
                        )
                    )
                    or proof.created_at > plan.read_at
                    or (next_revision == plan.target_revision and proof != plan.tail)
                ):
                    raise DomainError("CONFLICT", "Coverage source proof chain differs", 409)
                manifest = self.coverage.record_build_step_in_transaction(
                    db,
                    context,
                    plan,
                    proof,
                ).manifest
                draft = StoredCoverageBuildStep.model_construct(
                    plan_id=request.id,
                    plan_fingerprint=plan.fingerprint,
                    revision=next_revision,
                    previous_fingerprint=previous.fingerprint if previous else None,
                    attestation_id=proof.request.id,
                    attestation_fingerprint=proof.fingerprint,
                    first_seq=proof.first_seq,
                    last_seq=proof.last_seq,
                    manifest=WindowManifestSelection(
                        id=manifest.analysis.request.id, fingerprint=manifest.fingerprint
                    ),
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                step = StoredCoverageBuildStep.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": coverage_build_step_fingerprint(draft),
                    }
                )
                self._active(plan.configuration)
                self._event(
                    db,
                    context,
                    step.event_id,
                    "source_built",
                    request.id,
                    step.created_at,
                    step.fingerprint,
                    next_revision,
                )
                repo.append_step(step)
                db.flush()
                self._deadline(deadline)
                commit_owned_transaction(db)
                return step
        except DBAPIError as error:
            raise self._failure(error) from None

    def finish(self, context: UsageTraceConsumeContext, id: UUID) -> UsageWindowReceipt:
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                self._begin(db, snapshot=True)
                CoverageBuildRepository(db).lock(id)
                result = self.windows.capture_build_in_transaction(db, context, id)
                if not result.duplicate:
                    self._active(result.snapshot.window.configuration)
                    self._deadline(deadline)
                    commit_owned_transaction(db)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def completed_in_transaction(
        self, session: Session, target: AdmitCoverageWindow
    ) -> StoredUsageWindow:
        """Internal evidence lookup; callers still enforce their current access policy."""
        target = AdmitCoverageWindow.model_validate(target.model_dump(mode="json"))
        repo = CoverageBuildRepository(session)
        plan = repo.get(target.id)
        if plan is None:
            raise DomainError("NOT_FOUND", "Coverage build not found", 404)
        if (
            plan.request.query != target.query
            or plan.configuration.fingerprint != target.expected_configuration_fingerprint
        ):
            raise DomainError("CONFLICT", "Coverage result has different fixed input", 409)
        saved = UsageWindowRepository(session).get(plan.window_id)
        if saved is None:
            raise DomainError("COVERAGE_BUILD_PENDING", "Coverage snapshot is not saved", 409)
        last = repo.latest(target.id)
        if (last.revision if last else plan.start_revision) != plan.target_revision:
            raise DomainError("COVERAGE_BUILD_PENDING", "Coverage sources are not built", 409)
        if last is not None:
            self._saved_step(session, plan, last)
        if (
            saved.request.query != plan.request.query
            or saved.window.configuration != plan.configuration
            or saved.request.ledger != plan.request.ledger
            or saved.window.read_at < plan.read_at
            or saved.window.identity_revision != plan.identity_revision
            or saved.window.source != plan.source
            or saved.request.prefix_checkpoint != plan.request.prefix_checkpoint
            or {value.id for value in saved.request.manifests}
            != {
                plan.manifest_id(revision)
                for revision in range(plan.start_revision + 1, plan.target_revision + 1)
            }
        ):
            raise inconsistent_build()
        return saved

    def progress(self, id: UUID) -> CoverageBuildProgress:
        return self.inspect_progress(id)[1]

    def inspect_progress(self, id: UUID) -> tuple[StoredCoverageBuild, CoverageBuildProgress]:
        """Read the plan and verify its progress together for the report worker."""
        try:
            with self.sessions() as db:
                self._begin(db)
                repo = CoverageBuildRepository(db)
                plan = repo.get(id)
                if plan is None:
                    raise DomainError("NOT_FOUND", "Coverage build not found", 404)
                step = repo.latest(id)
                if step is not None:
                    self._saved_step(db, plan, step)
                revision = step.revision if step else plan.start_revision
                return plan, CoverageBuildProgress(
                    plan_id=id,
                    plan_fingerprint=plan.fingerprint,
                    processed_revision=revision,
                    target_revision=plan.target_revision,
                    completed_steps=revision - plan.start_revision,
                    total_steps=plan.target_revision - plan.start_revision,
                    last_step_at=step.created_at if step else None,
                    sources_built=revision == plan.target_revision,
                    window_id=plan.window_id,
                    window_saved=UsageWindowRepository(db).get(plan.window_id) is not None,
                    generated_at=self._now(),
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _event(
        db: Session,
        context: UsageTraceConsumeContext,
        id: UUID,
        action: str,
        plan_id: UUID,
        at: datetime,
        fingerprint: str,
        revision: int = 0,
    ) -> None:
        name = "lyra.usage.coverage_build_" + action
        payload = {
            "plan_id": str(plan_id),
            "fingerprint": fingerprint,
            "source_revision": revision,
            "quota_usage_available": False,
        }
        db.add(
            DomainEvent(
                event_id=str(id),
                event_type=name,
                event_version=1,
                aggregate_type="usage_coverage_build",
                aggregate_id=str(plan_id),
                aggregate_version=revision + 1,
                occurred_at=at,
                recorded_at=at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(plan_id),
                payload=payload,
            )
        )
        db.flush()
        db.add(OutboxEntry(event_id=str(id), created_at=at))
        db.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=name,
                target=str(plan_id),
                request_id=context.request_id,
                recorded_at=at,
                changes=payload,
            )
        )
