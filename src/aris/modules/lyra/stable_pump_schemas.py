"""One bounded stable-source, preparation and measurement iteration."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.recording_job_schemas import RecordingJobResult, RecordingWorkSelection
from aris.modules.lyra.recording_pump_schemas import RecordingSourceStep, RecordingStageFailure
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.source_batching import SourceBatchSelection
from aris.modules.lyra.stable_dispatch_schemas import StableAutoDispatchReceipt
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract


class StableRecordingPumpConfiguration(SourceBatchSelection):
    recording: UsagePostingConfiguration
    method: StableResidencyLayoutMethod
    source_limit: int = Field(default=32, ge=1, le=128, strict=True)
    dispatch_limit: int = Field(default=1, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def source_batch_fits(self) -> "StableRecordingPumpConfiguration":
        if (
            self.source_batching is not None
            and self.source_batching.minimum_messages > self.source_limit
        ):
            raise ValueError("source minimum batch exceeds the configured page limit")
        return self

    @property
    def selection(self) -> RecordingWorkSelection:
        return RecordingWorkSelection(
            recording_configuration_fingerprint=self.recording.fingerprint,
            partition_method_fingerprint=self.method.fingerprint,
            stable_method_fingerprint=self.method.fingerprint,
            include_closed_runs=False,
        )

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("stable_recording_pump_configuration", canonical_trace(self))


class StableRecordingPumpTick(IntervalContract):
    assessment_scope: Literal["bounded_stable_recording_pump_tick"] = (
        "bounded_stable_recording_pump_tick"
    )
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    paused: bool = Field(strict=True)
    generated_at: AwareDatetime
    source: RecordingSourceStep | None = None
    dispatch: StableAutoDispatchReceipt | None = None
    claimed_job_id: UUID | None = None
    job: RecordingJobResult | None = None
    failures: tuple[RecordingStageFailure, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def independent_stages(self) -> "StableRecordingPumpTick":
        if self.paused and (
            any(
                value is not None
                for value in (self.source, self.dispatch, self.claimed_job_id, self.job)
            )
            or self.failures
        ):
            raise ValueError("paused stable pump cannot report executed stages")
        if len({value.stage for value in self.failures}) != len(self.failures):
            raise ValueError("one failure per attempted stable stage is required")
        return self
