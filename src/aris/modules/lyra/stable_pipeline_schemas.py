"""Atomic stable-source preparation is separate from dispatch and measured coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.recording_pipeline_schemas import (
    MAX_PIPELINE_BATCH_BYTES,
    RecordingPipelineRequest,
)
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.usage_intervals import IntervalContract


class StableRecordingPipelineConfiguration(IntervalContract):
    source_ref: str = Field(min_length=1, max_length=128)
    trace: UsageTraceConfiguration
    recording: UsagePostingConfiguration
    method: StableResidencyLayoutMethod
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)

    @model_validator(mode="after")
    def one_timing_method(self) -> "StableRecordingPipelineConfiguration":
        if self.recording.timing_fingerprint != residency_fingerprint(
            self.trace.continuity, self.trace.formation
        ):
            raise ValueError("pipeline trace and recording timing differ")
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("stable_recording_pipeline_configuration", canonical_trace(self))


class StableRunPreparation(IntervalContract):
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_at_record: Literal["open", "closed"]
    observed_intervals: int = Field(ge=1, le=9223372036854775807, strict=True)
    state: Literal["tracked", "needs_review"]
    layout_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    plan_version: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)
    plan_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    code: Literal["USAGE_NEEDS_REVIEW"] | None = None

    @model_validator(mode="after")
    def fixed_disposition(self) -> "StableRunPreparation":
        bound = (self.layout_key, self.plan_version, self.plan_fingerprint)
        if (
            (self.state == "tracked") != all(value is not None for value in bound)
            or (self.state != "tracked" and any(value is not None for value in bound))
            or self.code
            != {
                "tracked": None,
                "needs_review": "USAGE_NEEDS_REVIEW",
            }[self.state]
        ):
            raise ValueError("stable run preparation differs from its observed disposition")
        return self


class StoredStableRecordingPipelineBatch(IntervalContract):
    assessment_scope: Literal["atomic_stable_source_preparation_batch"] = (
        "atomic_stable_source_preparation_batch"
    )
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: RecordingPipelineRequest
    configuration: StableRecordingPipelineConfiguration
    source_record_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    first_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    last_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    source_count: int = Field(ge=1, le=128, strict=True)
    source_cutoff: AwareDatetime
    attestation_id: UUID
    attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    segment_set_id: UUID
    segment_set_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcomes: tuple[StableRunPreparation, ...] = Field(max_length=512)
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("source_cutoff", "created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("outcomes")
    @classmethod
    def canonical_outcomes(
        cls, values: tuple[StableRunPreparation, ...]
    ) -> tuple[StableRunPreparation, ...]:
        if len({value.run_id for value in values}) != len(values):
            raise ValueError("one preparation outcome is required per observed residency run")
        if sum(value.state == "tracked" for value in values) > 16:
            raise ValueError("too many prepared plans in one source batch")
        return tuple(sorted(values, key=lambda value: value.run_id))

    @model_validator(mode="after")
    def fixed_atomic_batch(self) -> "StoredStableRecordingPipelineBatch":
        source = self.request.source
        if (
            self.request.expected_configuration_fingerprint != self.configuration.fingerprint
            or source.expected_configuration_fingerprint != self.configuration.trace.fingerprint
            or self.revision != source.expected_revision + 1
            or self.first_seq > self.last_seq
            or self.last_seq > source.through_seq
            or self.source_count != self.last_seq - self.first_seq + 1
            or self.source_count > source.limit
            or self.source_cutoff > self.created_at
            or self.attestation_id != self.request.child_id("attestation")
            or self.segment_set_id != self.request.child_id("segments")
            or self.fingerprint != stable_pipeline_batch_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_PIPELINE_BATCH_BYTES
        ):
            raise ValueError("pipeline batch differs from its fixed source and configuration")
        return self


class StableRecordingPipelineReceipt(IntervalContract):
    batch: StoredStableRecordingPipelineBatch
    duplicate: bool = Field(strict=True)


def stable_pipeline_batch_fingerprint(value: StoredStableRecordingPipelineBatch) -> str:
    return trace_fingerprint(
        "stable_pipeline_batch", canonical_trace(value, exclude={"fingerprint"})
    )
