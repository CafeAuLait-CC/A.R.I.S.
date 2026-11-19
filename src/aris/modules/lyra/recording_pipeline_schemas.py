"""Atomic source preparation does not assert recording or time-window coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    UsageTraceConsumeRequest,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_PIPELINE_BATCH_BYTES = 131072
_CHILD_NAMESPACE = UUID("2f7cb513-848a-4379-ae3c-41667e150a84")


class RecordingPipelineConfiguration(IntervalContract):
    source_ref: str = Field(min_length=1, max_length=128)
    trace: UsageTraceConfiguration
    recording: UsagePostingConfiguration
    method: ResidencyPartitionMethod
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)

    @model_validator(mode="after")
    def one_timing_method(self) -> "RecordingPipelineConfiguration":
        if self.recording.timing_fingerprint != residency_fingerprint(
            self.trace.continuity, self.trace.formation
        ):
            raise ValueError("pipeline trace and recording timing differ")
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("recording_pipeline_configuration", canonical_trace(self))


class RecordingPipelineLimits(IntervalContract):
    max_plans_per_batch: int = Field(default=8, ge=1, le=16, strict=True)
    timeout_ms: int = Field(default=30000, ge=100, le=30000, strict=True)


class RecordingPipelineRequest(IntervalContract):
    source: UsageTraceConsumeRequest
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    def child_id(self, kind: str) -> UUID:
        return uuid5(_CHILD_NAMESPACE, str(self.source.id) + ":" + kind)


class ClosedRunPreparation(IntervalContract):
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["planned", "not_qualified", "needs_review"]
    plan_id: UUID | None = None
    plan_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    code: Literal["RESIDENCY_NOT_STABLE", "USAGE_NEEDS_REVIEW"] | None = None

    @model_validator(mode="after")
    def explicit_disposition(self) -> "ClosedRunPreparation":
        if (
            (self.state == "planned")
            != (self.plan_id is not None and self.plan_fingerprint is not None)
            or (
                self.state != "planned"
                and (self.plan_id is not None or self.plan_fingerprint is not None)
            )
            or self.code
            != {
                "planned": None,
                "not_qualified": "RESIDENCY_NOT_STABLE",
                "needs_review": "USAGE_NEEDS_REVIEW",
            }[self.state]
        ):
            raise ValueError("closed-run preparation is not an explicit disposition")
        return self


class StoredRecordingPipelineBatch(IntervalContract):
    assessment_scope: Literal["atomic_source_preparation_batch"] = "atomic_source_preparation_batch"
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: RecordingPipelineRequest
    configuration: RecordingPipelineConfiguration
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
    outcomes: tuple[ClosedRunPreparation, ...] = Field(max_length=256)
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
        cls, values: tuple[ClosedRunPreparation, ...]
    ) -> tuple[ClosedRunPreparation, ...]:
        if len({value.run_id for value in values}) != len(values):
            raise ValueError("one preparation outcome is required per closed run")
        if sum(value.state == "planned" for value in values) > 16:
            raise ValueError("too many prepared plans in one source batch")
        return tuple(sorted(values, key=lambda value: value.run_id))

    @model_validator(mode="after")
    def fixed_atomic_batch(self) -> "StoredRecordingPipelineBatch":
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
            or any(
                value.plan_id != self.request.child_id("plan:" + value.run_id)
                for value in self.outcomes
                if value.state == "planned"
            )
            or self.fingerprint != pipeline_batch_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_PIPELINE_BATCH_BYTES
        ):
            raise ValueError("pipeline batch differs from its fixed source and configuration")
        return self


class RecordingPipelineReceipt(IntervalContract):
    batch: StoredRecordingPipelineBatch
    duplicate: bool = Field(strict=True)


def pipeline_batch_fingerprint(value: StoredRecordingPipelineBatch) -> str:
    return trace_fingerprint(
        "recording_pipeline_batch", canonical_trace(value, exclude={"fingerprint"})
    )
