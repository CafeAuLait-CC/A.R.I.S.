"""Durable dispatch progress is neither measured usage nor source coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.partition_recording_schemas import PartitionRecordingRequest
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_RECORDING_PLAN_BYTES = 16384
MAX_DISPATCH_JOBS = 8
_JOB_NAMESPACE = UUID("520e65fd-f881-41b2-94d1-39e202f8481b")


class RecordingPlanRequest(PartitionRecordingRequest):
    @model_validator(mode="after")
    def starts_at_zero(self) -> "RecordingPlanRequest":
        if self.partition.partition_index != 0:
            raise ValueError("a recording plan starts at partition zero")
        return self


class StoredRecordingPlan(IntervalContract):
    assessment_scope: Literal["fixed_partition_dispatch_plan"] = "fixed_partition_dispatch_plan"
    quota_usage_available: Literal[False] = False
    request: RecordingPlanRequest
    parent_run: ResidencyRun = Field(repr=False)
    consumer_id: str = Field(min_length=1, max_length=128)
    method: ResidencyPartitionMethod
    configuration: UsagePostingConfiguration
    partition_count: int = Field(ge=1, le=9223372036854775807, strict=True)
    first_partition_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fixed_dispatch_input(self) -> "StoredRecordingPlan":
        run = self.parent_run
        if (
            run.id != self.request.partition.reference.run_id
            or self.request.partition.reference.source_cutoff > self.created_at
            or run.last.compute_observed_at > self.request.partition.reference.source_cutoff
            or run.first.compute_observed_at < self.configuration.recording_start_at
            or run.rule_fingerprint != self.configuration.timing_fingerprint
            or self.configuration.fingerprint
            != self.request.expected_recording_configuration_fingerprint
            or self.method.fingerprint != self.request.partition.expected_method_fingerprint
            or self.partition_count
            != (run.intervals + self.method.max_intervals - 1) // self.method.max_intervals
            or self.fingerprint != recording_plan_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_RECORDING_PLAN_BYTES
        ):
            raise ValueError("recording plan differs from its fixed parent or configuration")
        return self

    def job(self, index: int) -> PartitionRecordingRequest:
        if type(index) is not int or not 0 <= index < self.partition_count:
            raise ValueError("partition index is outside the fixed plan")
        return PartitionRecordingRequest.model_validate(
            {
                **self.request.model_dump(mode="json"),
                "id": str(uuid5(_JOB_NAMESPACE, str(self.request.id) + ":" + str(index))),
                "partition": self.request.partition.model_copy(
                    update={"partition_index": index}
                ).model_dump(mode="json"),
            }
        )


class RecordingPlanReceipt(IntervalContract):
    plan: StoredRecordingPlan
    duplicate: bool = Field(strict=True)


class RecordingPlanProgress(IntervalContract):
    assessment_scope: Literal["partition_job_dispatch_progress"] = "partition_job_dispatch_progress"
    quota_usage_available: Literal[False] = False
    plan: StoredRecordingPlan
    version: int = Field(ge=1, le=9223372036854775807, strict=True)
    next_partition: int = Field(ge=0, le=9223372036854775807, strict=True)
    updated_at: AwareDatetime
    all_enqueued: bool = Field(strict=True)

    @field_validator("updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def progress_is_only_dispatch(self) -> "RecordingPlanProgress":
        if (
            self.next_partition > self.plan.partition_count
            or self.all_enqueued != (self.next_partition == self.plan.partition_count)
            or self.updated_at < self.plan.created_at
            or (self.version == 1) != (self.next_partition == 0)
            or self.version > self.next_partition + 1
        ):
            raise ValueError("recording plan dispatch progress is inconsistent")
        return self


class RecordingDispatchRequest(IntervalContract):
    plan_id: UUID
    expected_version: int = Field(ge=1, le=9223372036854775807, strict=True)
    limit: int = Field(default=1, ge=1, le=MAX_DISPATCH_JOBS, strict=True)


class RecordingDispatchReceipt(IntervalContract):
    progress: RecordingPlanProgress
    job_ids: tuple[UUID, ...] = Field(max_length=MAX_DISPATCH_JOBS)


def recording_plan_fingerprint(value: StoredRecordingPlan) -> str:
    return trace_fingerprint("recording_plan", canonical_trace(value, exclude={"fingerprint"}))
