"""A bounded live-identity stable source/preparation/recording loop, opt-in only."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid5

from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_schemas import UsagePostingContext
from aris.modules.lyra.recording_job_schemas import RecordingJobClaim, RecordingJobResult
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.recording_pipeline_schemas import RecordingPipelineRequest
from aris.modules.lyra.recording_pump import SourceCeilingReader
from aris.modules.lyra.recording_pump_schemas import RecordingSourceStep, RecordingStageFailure
from aris.modules.lyra.stable_dispatch_service import StableWindowDispatcher
from aris.modules.lyra.stable_pipeline_service import StableRecordingPipelineService
from aris.modules.lyra.stable_pump_schemas import (
    StableRecordingPumpConfiguration,
    StableRecordingPumpTick,
)
from aris.modules.lyra.stable_window_plan_service import StableWindowPlanService
from aris.modules.lyra.trace_consumer_schemas import TraceConsumptionIdle, UsageTraceConsumeRequest
from aris.modules.vega.usage_archive_schemas import UsageArchiveHead, UsageArchiveHeadQuery

_SOURCE_NAMESPACE = UUID("b1f3831f-884b-54b8-8e4e-8aba39be1f63")


class StableRecordingPump:
    def __init__(
        self,
        pipelines: tuple[StableRecordingPipelineService, ...],
        archive: SourceCeilingReader,
        plans: StableWindowPlanService,
        jobs: RecordingJobService,
        *,
        configuration: StableRecordingPumpConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(pipelines) > 16:
            raise ValueError("one recording pump supports at most 16 configured sources")
        self.pipelines, self.archive, self.plans, self.jobs = tuple(pipelines), archive, plans, jobs
        self.configuration = (
            StableRecordingPumpConfiguration.model_validate(configuration.model_dump(mode="json"))
            if configuration is not None
            else None
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self._source_index = 0
        self._source_limits: dict[str, int] = {}
        self.dispatcher = StableWindowDispatcher(plans)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("pump clock requires a timezone")
        return now.astimezone(UTC)

    def _cohort(self, expected: StableRecordingPumpConfiguration) -> None:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Stable recording pump is paused", 503)
        current = StableRecordingPumpConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        handler = self.jobs.stable_recorder
        if (
            current != expected
            or self.plans.jobs is not self.jobs
            or self.plans.configuration != current.recording
            or handler is None
            or handler.acceptances is not self.plans.acceptances
            or handler.ledger.configuration != current.recording
            or handler.acceptances.evidence is not self.plans.evidence
            or self.plans.evidence.method != current.method
        ):
            raise DomainError("CONFLICT", "Stable pump authority or configuration differs", 409)
        bindings = []
        for producer in self.pipelines:
            source = producer.configuration
            if source is not None:
                if (
                    source.recording != current.recording
                    or source.method != current.method
                    or producer.plans is not self.plans
                    or producer.identity_head is None
                ):
                    raise DomainError(
                        "CONFLICT", "Stable source cohort or identity reader differs", 409
                    )
                bindings.append((source.trace.agent_id, source.trace.node_id))
        if len(set(bindings)) != len(bindings):
            raise DomainError("CONFLICT", "Stable pump repeats a source node", 409)

    @staticmethod
    def _failure(
        stage: str, error: DomainError, consumer_id: str | None = None
    ) -> RecordingStageFailure:
        code = error.code if re.fullmatch(r"[A-Z][A-Z_]{1,63}", error.code) else "PROCESSING_FAILED"
        return RecordingStageFailure.model_validate(
            {"stage": stage, "code": code, "consumer_id": consumer_id}
        )

    def _source(
        self,
        context: UsagePostingContext,
        producer: StableRecordingPipelineService,
        config: StableRecordingPumpConfiguration,
    ) -> RecordingSourceStep:
        source = producer.refresh_identity_configuration()
        if source is None:
            return RecordingSourceStep(state="paused")
        source_limit = min(
            config.source_limit,
            self._source_limits.get(source.trace.consumer_id, config.source_limit),
        )
        query = UsageArchiveHeadQuery(agent_id=source.trace.agent_id, node_id=source.trace.node_id)
        head = self.archive.head(query)
        head = UsageArchiveHead.model_validate(head.model_dump(mode="json"))
        if head.query != query or head.read_at > self._now():
            raise DomainError("CONFLICT", "Recording source ceiling differs", 409)
        position = producer.trace.position()
        if (
            position.configuration != source.trace
            or head.highest_committed_seq < position.after_seq
        ):
            raise DomainError(
                "CONFLICT", "Recording source position exceeds its committed ceiling", 409
            )
        through = head.highest_committed_seq
        if through == position.after_seq:
            return RecordingSourceStep(
                state="caught_up",
                consumer_id=source.trace.consumer_id,
                after_seq=position.after_seq,
                ceiling=through,
            )
        key = (
            source.fingerprint
            + ":"
            + str(position.revision)
            + ":"
            + str(through)
            + ":"
            + str(source_limit)
        )
        request = RecordingPipelineRequest(
            source=UsageTraceConsumeRequest(
                id=uuid5(_SOURCE_NAMESPACE, key),
                expected_configuration_fingerprint=source.trace.fingerprint,
                expected_revision=position.revision,
                through_seq=through,
                limit=source_limit,
            ),
            expected_configuration_fingerprint=source.fingerprint,
        )
        result = producer.consume(context, request)
        if isinstance(result, TraceConsumptionIdle):
            return RecordingSourceStep(
                state=result.state,
                consumer_id=source.trace.consumer_id,
                after_seq=result.position.after_seq,
                ceiling=through,
                missing_seq=result.missing_seq,
            )
        batch = result.batch
        if batch.request != request or batch.configuration != source:
            raise DomainError("CONFLICT", "Recording source batch binding differs", 409)
        return RecordingSourceStep(
            state="prepared",
            consumer_id=source.trace.consumer_id,
            batch_id=batch.request.source.id,
            after_seq=batch.last_seq,
            ceiling=through,
            missing_seq=batch.last_seq + 1
            if batch.last_seq < through and batch.source_count < source_limit
            else None,
        )

    def tick(self, context: UsagePostingContext) -> StableRecordingPumpTick:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        if self.configuration is None:
            return StableRecordingPumpTick(paused=True, generated_at=self._now())
        config = StableRecordingPumpConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        self._cohort(config)
        source_step = None
        dispatched = None
        job_result = None
        claimed_id = None
        failures = []
        if self.pipelines:
            producer = self.pipelines[self._source_index % len(self.pipelines)]
            self._source_index += 1
            try:
                source_step = self._source(context, producer, config)
                self._cohort(config)
            except DomainError as error:
                consumer = (
                    producer.configuration.trace.consumer_id
                    if producer.configuration is not None
                    else None
                )
                if error.code == "PIPELINE_BATCH_TOO_LARGE" and consumer is not None:
                    previous = self._source_limits.get(consumer, config.source_limit)
                    self._source_limits[consumer] = max(1, previous // 2)
                failures.append(self._failure("source", error, consumer))
        try:
            self._cohort(config)
            dispatched = self.dispatcher.dispatch_next(
                context, config.selection, limit=config.dispatch_limit
            )
            self._cohort(config)
        except DomainError as error:
            failures.append(self._failure("dispatch", error))
        try:
            self._cohort(config)
            claim = self.jobs.claim(context, selection=config.selection)
            if isinstance(claim, RecordingJobClaim):
                claimed_id = claim.id
                self._cohort(config)
                job_result = self.jobs.execute(context, claim)
            elif isinstance(claim, RecordingJobResult):
                job_result = claim
            self._cohort(config)
        except DomainError as error:
            failures.append(self._failure("execute", error))
        return StableRecordingPumpTick(
            paused=False,
            generated_at=self._now(),
            source=source_step,
            dispatch=dispatched,
            claimed_job_id=claimed_id,
            job=job_result,
            failures=tuple(failures),
        )
