"""Fixed internal work is queued, never represented as completed usage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from aris.modules.lyra.partition_recording_schemas import PartitionRecordingRequest
from aris.modules.lyra.run_recording_schemas import ClosedRunRecordingRequest
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_RECORDING_JOB_INPUT_BYTES = 16384
JobState = Literal["queued", "running", "completed", "blocked", "failed"]
RecordingRequest = ClosedRunRecordingRequest | PartitionRecordingRequest
recording_request_adapter: TypeAdapter[RecordingRequest] = TypeAdapter(RecordingRequest)


class RecordingWorkSelection(IntervalContract):
    recording_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    partition_method_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class RecordingJobLimits(IntervalContract):
    transaction_timeout_ms: int = Field(ge=1001, le=30000, strict=True)
    lease_seconds: int = Field(ge=2, le=600, strict=True)
    max_attempts: int = Field(ge=1, le=20, strict=True)
    retry_base_seconds: int = Field(ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def lease_exceeds_transaction(self) -> "RecordingJobLimits":
        if self.transaction_timeout_ms >= self.lease_seconds * 1000:
            raise ValueError("recording job lease must exceed its entire transaction budget")
        return self


class StoredRecordingJob(IntervalContract):
    assessment_scope: Literal["fixed_recording_job"] = "fixed_recording_job"
    quota_usage_available: Literal[False] = False
    request: RecordingRequest = Field(repr=False)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: JobState
    version: int = Field(ge=1, le=9223372036854775807, strict=True)
    attempts: int = Field(ge=0, le=20, strict=True)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    available_at: AwareDatetime
    lease_token: UUID | None
    lease_expires_at: AwareDatetime | None
    last_error_code: str | None = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    finished_at: AwareDatetime | None
    recording_id: UUID | None
    recording_fingerprint: str | None = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at", "updated_at", "available_at", "lease_expires_at", "finished_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def exact_job_state(self) -> "StoredRecordingJob":
        running = self.state == "running"
        terminal = self.state in {"completed", "blocked", "failed"}
        complete = self.state == "completed"
        if (
            self.request_fingerprint != recording_job_fingerprint(self.request)
            or running != (self.lease_token is not None)
            or running != (self.lease_expires_at is not None)
            or terminal != (self.finished_at is not None)
            or complete != (self.recording_id is not None)
            or complete != (self.recording_fingerprint is not None)
            or (complete and self.recording_id != self.request.id)
            or (running and self.attempts == 0)
            or self.updated_at < self.created_at
            or self.available_at < self.created_at
            or (self.finished_at is not None and self.finished_at != self.updated_at)
            or (self.lease_expires_at is not None and self.lease_expires_at <= self.updated_at)
        ):
            raise ValueError("recording job differs from its fixed input or lease state")
        return self


class RecordingJobEnqueueReceipt(IntervalContract):
    job: StoredRecordingJob
    duplicate: bool = Field(strict=True)


class RecordingJobClaim(IntervalContract):
    id: UUID
    version: int = Field(ge=2, le=9223372036854775807, strict=True)
    attempt: int = Field(ge=1, le=20, strict=True)
    lease_token: UUID
    lease_expires_at: AwareDatetime


class RecordingJobResult(IntervalContract):
    id: UUID
    state: Literal["completed", "blocked", "failed", "retry", "lost_claim"]
    code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    recording_id: UUID | None = None
    recording_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    quota_usage_available: Literal[False] = False

    @model_validator(mode="after")
    def completed_reference(self) -> "RecordingJobResult":
        if (
            (self.state == "completed") != (self.recording_id is not None)
            or (self.recording_id is None) != (self.recording_fingerprint is None)
            or (self.recording_id is not None and self.recording_id != self.id)
        ):
            raise ValueError(
                "completed recording job requires its exact immutable result reference"
            )
        return self


def recording_job_fingerprint(request: RecordingRequest) -> str:
    return trace_fingerprint("recording_job_input", canonical_trace(request))


def recording_source_cutoff(request: RecordingRequest) -> datetime:
    return (
        request.reference.source_cutoff
        if isinstance(request, ClosedRunRecordingRequest)
        else request.partition.reference.source_cutoff
    )
