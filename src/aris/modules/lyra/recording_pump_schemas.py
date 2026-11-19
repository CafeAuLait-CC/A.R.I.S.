"""One bounded preparation/dispatch/execution iteration, never quota coverage."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.recording_job_schemas import RecordingJobResult, RecordingWorkSelection
from aris.modules.lyra.recording_plan_schemas import RecordingDispatchReceipt
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract


class RecordingPumpConfiguration(IntervalContract):
    recording: UsagePostingConfiguration
    method: ResidencyPartitionMethod
    source_limit: int = Field(default=32, ge=1, le=128, strict=True)
    dispatch_limit: int = Field(default=1, ge=1, le=8, strict=True)

    @property
    def selection(self) -> RecordingWorkSelection:
        return RecordingWorkSelection(
            recording_configuration_fingerprint=self.recording.fingerprint,
            partition_method_fingerprint=self.method.fingerprint,
        )

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("recording_pump_configuration", canonical_trace(self))


class RecordingSourceStep(IntervalContract):
    state: Literal["prepared", "caught_up", "gap", "paused"]
    consumer_id: str | None = Field(default=None, min_length=1, max_length=128)
    after_seq: int | None = Field(default=None, ge=0, strict=True)
    ceiling: int | None = Field(default=None, ge=0, strict=True)
    batch_id: UUID | None = None
    missing_seq: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def actual_source_progress(self) -> "RecordingSourceStep":
        if self.state == "paused":
            if any(
                value is not None
                for value in (
                    self.consumer_id,
                    self.after_seq,
                    self.ceiling,
                    self.batch_id,
                    self.missing_seq,
                )
            ):
                raise ValueError("paused source cannot report progress")
        elif (
            self.consumer_id is None
            or self.after_seq is None
            or self.ceiling is None
            or self.after_seq > self.ceiling
            or (self.state == "prepared") != (self.batch_id is not None)
            or (
                self.state == "caught_up"
                and (self.after_seq != self.ceiling or self.missing_seq is not None)
            )
            or (self.state == "gap" and self.missing_seq is None)
            or (
                self.missing_seq is not None
                and (self.missing_seq != self.after_seq + 1 or self.after_seq >= self.ceiling)
            )
        ):
            raise ValueError("source step differs from its actual bounded progress")
        return self


class RecordingStageFailure(IntervalContract):
    stage: Literal["source", "dispatch", "execute"]
    code: str = Field(pattern=r"^[A-Z][A-Z_]{1,63}$")
    consumer_id: str | None = Field(default=None, min_length=1, max_length=128)


class RecordingPumpTick(IntervalContract):
    assessment_scope: Literal["bounded_recording_pump_tick"] = "bounded_recording_pump_tick"
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    paused: bool = Field(strict=True)
    generated_at: AwareDatetime
    source: RecordingSourceStep | None = None
    dispatch: RecordingDispatchReceipt | None = None
    claimed_job_id: UUID | None = None
    job: RecordingJobResult | None = None
    failures: tuple[RecordingStageFailure, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def independent_committed_stages(self) -> "RecordingPumpTick":
        if self.paused and (
            any(
                value is not None
                for value in (self.source, self.dispatch, self.claimed_job_id, self.job)
            )
            or self.failures
        ):
            raise ValueError("paused pump cannot report executed stages")
        if len({value.stage for value in self.failures}) != len(self.failures):
            raise ValueError("one failure per attempted pump stage is required")
        return self
