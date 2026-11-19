"""Prepare one bounded source batch and all closed-run dispositions atomically."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingContext
from aris.modules.lyra.recording_pipeline_repo import RecordingPipelineRepository
from aris.modules.lyra.recording_pipeline_schemas import (
    ClosedRunPreparation,
    RecordingPipelineConfiguration,
    RecordingPipelineLimits,
    RecordingPipelineReceipt,
    RecordingPipelineRequest,
    StoredRecordingPipelineBatch,
    pipeline_batch_fingerprint,
)
from aris.modules.lyra.recording_plan_schemas import RecordingPlanRequest
from aris.modules.lyra.recording_plan_service import RecordingPlanService
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionReference
from aris.modules.lyra.residency_segments import ResidencySegmentQuery
from aris.modules.lyra.segment_store_schemas import RecordResidencySegments
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.lyra.trace_attestation_schemas import TraceAttestationRequest
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    TraceConsumptionIdle,
    UsageTraceConsumeContext,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService


class RecordingPipelineService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        trace: UsageTraceConsumerService,
        attestations: TraceAttestationService,
        segments: ResidencySegmentStore,
        plans: RecordingPlanService,
        *,
        configuration: RecordingPipelineConfiguration | None = None,
        limits: RecordingPipelineLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.trace, self.attestations, self.segments, self.plans = (
            sessions,
            trace,
            attestations,
            segments,
            plans,
        )
        self.configuration = (
            RecordingPipelineConfiguration.model_validate(configuration.model_dump(mode="json"))
            if configuration is not None
            else None
        )
        self.limits = RecordingPipelineLimits.model_validate(
            (limits or RecordingPipelineLimits()).model_dump(mode="json")
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("pipeline clock requires a timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Pipeline database is unavailable",
            503,
        )

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Pipeline batch budget exceeded", 503)

    def _active(self, request: RecordingPipelineRequest) -> RecordingPipelineConfiguration:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Recording pipeline is paused", 503)
        config = RecordingPipelineConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        if (
            config.fingerprint != request.expected_configuration_fingerprint
            or config.trace.fingerprint != request.source.expected_configuration_fingerprint
            or self.trace.configuration != config.trace
            or self.plans.ledger.configuration != config.recording
        ):
            raise DomainError("CONFLICT", "Pipeline configuration differs", 409)
        return config

    def _stored(self, session: Session, id: UUID) -> StoredRecordingPipelineBatch | None:
        result = RecordingPipelineRepository(session).get(id)
        if result is not None:
            source = UsageTraceRepository(session).get(id)
            if (
                source is None
                or source.request != result.request.source
                or source.before.configuration != result.configuration.trace
                or {value.run.id for value in source.closed_runs}
                != {value.run_id for value in result.outcomes}
            ):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Pipeline source binding differs", 503)
            closed = {value.run.id: value for value in source.closed_runs}
            for outcome in result.outcomes:
                if (outcome.state == "not_qualified") != (
                    closed[outcome.run_id].state != "stable_candidate"
                ):
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "Pipeline disposition differs", 503)
                if outcome.state == "planned":
                    if outcome.plan_id is None:
                        raise DomainError("DEPENDENCY_UNAVAILABLE", "Pipeline plan is missing", 503)
                    plan = self.plans.progress_in_transaction(session, outcome.plan_id).plan
                    if (
                        plan.fingerprint != outcome.plan_fingerprint
                        or plan.parent_run.id != outcome.run_id
                        or plan.parent_run != closed[outcome.run_id].run
                        or plan.consumer_id != result.configuration.trace.consumer_id
                        or plan.method != result.configuration.method
                        or plan.request.partition.closing_attestation_id != result.attestation_id
                        or plan.request.partition.expected_closing_attestation_fingerprint
                        != result.attestation_fingerprint
                        or plan.request.partition.reference.record_id != result.request.source.id
                        or plan.request.partition.reference.source_cutoff != result.source_cutoff
                        or plan.request.expected_recording_configuration_fingerprint
                        != result.configuration.recording.fingerprint
                        or plan.request.identity_revision != result.configuration.identity_revision
                    ):
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE", "Pipeline plan binding differs", 503
                        )
        return result

    def exact(self, id: UUID) -> StoredRecordingPipelineBatch:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                value = self._stored(session, id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Pipeline batch was not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def consume(
        self, context: UsagePostingContext, request: RecordingPipelineRequest
    ) -> RecordingPipelineReceipt | TraceConsumptionIdle:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.limits.timeout_ms)
                value = self.consume_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def consume_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordingPipelineRequest
    ) -> RecordingPipelineReceipt | TraceConsumptionIdle:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = RecordingPipelineRequest.model_validate(request.model_dump(mode="json"))
        limits = RecordingPipelineLimits.model_validate(self.limits.model_dump(mode="json"))
        deadline = monotonic() + limits.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = RecordingPipelineRepository(session)
                repo.lock(request.source.id)
                prior = self._stored(session, request.source.id)
                if prior is not None:
                    if prior.request != request:
                        raise DomainError("CONFLICT", "Pipeline operation has different input", 409)
                    return RecordingPipelineReceipt(batch=prior, duplicate=True)
                config = self._active(request)
                source_context = UsageTraceConsumeContext(request_id=context.request_id)
                consumed = self.trace.consume_in_transaction(
                    session, source_context, request.source
                )
                if isinstance(consumed, TraceConsumptionIdle):
                    if self._active(request) != config or self.limits != limits:
                        raise DomainError("CONFLICT", "Pipeline configuration changed", 409)
                    self._deadline(deadline)
                    return consumed
                record = UsageTraceRepository(session).get(consumed.id)
                cutoff = self._now()
                if record is None or record.created_at > cutoff:
                    raise DomainError("CONFLICT", "Pipeline source clock or binding differs", 409)
                proof = self.attestations.record_in_transaction(
                    session,
                    source_context,
                    TraceAttestationRequest(
                        id=request.child_id("attestation"),
                        record_id=record.request.id,
                        expected_record_fingerprint=record.fingerprint,
                        source_cutoff=cutoff,
                    ),
                ).attestation
                saved = self.segments.record_in_transaction(
                    session,
                    context,
                    RecordResidencySegments(
                        id=request.child_id("segments"),
                        source=ResidencySegmentQuery(
                            attestation_id=proof.request.id,
                            expected_attestation_fingerprint=proof.fingerprint,
                        ),
                    ),
                ).value
                if (
                    sum(item.state == "stable_candidate" for item in record.closed_runs)
                    > limits.max_plans_per_batch
                ):
                    raise DomainError(
                        "PIPELINE_BATCH_TOO_LARGE", "Closed runs exceed the bounded plan batch", 503
                    )
                outcomes = []
                for closed in record.closed_runs:
                    self._deadline(deadline)
                    run = closed.run
                    if closed.state != "stable_candidate":
                        outcomes.append(
                            ClosedRunPreparation(
                                run_id=run.id, state="not_qualified", code="RESIDENCY_NOT_STABLE"
                            )
                        )
                        continue
                    incoming = RecordingPlanRequest(
                        id=request.child_id("plan:" + run.id),
                        partition=ResidencyPartitionReference(
                            reference=ResidencyRunReference(
                                record_id=record.request.id,
                                expected_record_fingerprint=record.fingerprint,
                                run_id=run.id,
                                source_cutoff=cutoff,
                            ),
                            closing_attestation_id=proof.request.id,
                            expected_closing_attestation_fingerprint=proof.fingerprint,
                            expected_trace_configuration_fingerprint=config.trace.fingerprint,
                            expected_method_fingerprint=config.method.fingerprint,
                            partition_index=0,
                        ),
                        expected_recording_configuration_fingerprint=config.recording.fingerprint,
                        identity_revision=config.identity_revision,
                    )
                    try:
                        plan = self.plans.create_in_transaction(session, context, incoming).plan
                    except DomainError as error:
                        if error.code != "USAGE_NEEDS_REVIEW":
                            raise
                        outcomes.append(
                            ClosedRunPreparation(
                                run_id=run.id, state="needs_review", code="USAGE_NEEDS_REVIEW"
                            )
                        )
                    else:
                        outcomes.append(
                            ClosedRunPreparation(
                                run_id=run.id,
                                state="planned",
                                plan_id=plan.request.id,
                                plan_fingerprint=plan.fingerprint,
                            )
                        )
                now = self._now()
                if now < cutoff:
                    raise DomainError("CONFLICT", "Pipeline clock moved backwards", 409)
                unchecked = StoredRecordingPipelineBatch.model_construct(
                    request=request,
                    configuration=config,
                    source_record_fingerprint=record.fingerprint,
                    revision=record.position.revision,
                    first_seq=record.before.after_seq + 1,
                    last_seq=record.position.after_seq,
                    source_count=len(record.sources),
                    source_cutoff=cutoff,
                    attestation_id=proof.request.id,
                    attestation_fingerprint=proof.fingerprint,
                    segment_set_id=saved.request.id,
                    segment_set_fingerprint=saved.fingerprint,
                    outcomes=tuple(sorted(outcomes, key=lambda value: value.run_id)),
                    event_id=uuid4(),
                    created_at=now,
                    fingerprint="0" * 64,
                )
                value = StoredRecordingPipelineBatch.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": pipeline_batch_fingerprint(unchecked),
                    }
                )
                self._emit(session, context, value)
                session.flush()
                repo.append(value)
                session.flush()
                if self._active(request) != config or self.limits != limits:
                    raise DomainError("CONFLICT", "Pipeline configuration changed", 409)
                if self._now() < value.created_at:
                    raise DomainError("CONFLICT", "Pipeline clock moved backwards", 409)
                self._deadline(deadline)
                if self._stored(session, request.source.id) != value:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Pipeline batch was not stored", 503
                    )
                return RecordingPipelineReceipt(batch=value, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Pipeline source is inconsistent", 503
            ) from None

    @staticmethod
    def _emit(
        session: Session, context: UsagePostingContext, value: StoredRecordingPipelineBatch
    ) -> None:
        action = "lyra.usage.source_batch_prepared"
        event_id = str(value.event_id)
        payload = {
            "batch_id": str(value.request.source.id),
            "consumer_id": value.configuration.trace.consumer_id,
            "revision": value.revision,
            "first_seq": value.first_seq,
            "last_seq": value.last_seq,
            "closed_run_count": len(value.outcomes),
            "planned_count": sum(item.state == "planned" for item in value.outcomes),
            "fingerprint": value.fingerprint,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="usage_pipeline_batch",
                aggregate_id=str(value.request.source.id),
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.source.id),
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
                target=str(value.request.source.id),
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
