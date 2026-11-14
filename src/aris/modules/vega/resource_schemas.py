"""Component observations with independent quality and timestamps."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.contracts import Contract
from aris.modules.vega.schemas import AgentEnvelope, GpuSample, NodeStatus

Quality = Literal["ok", "error", "unsupported"]


class TimedObservation(Contract):
    observed_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class CpuObservation(Contract):
    quality: Literal["ok", "warming", "error", "unsupported"]
    logical_count: int | None = Field(default=None, ge=1)
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    window_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_values(self) -> "CpuObservation":
        if self.quality == "ok":
            if self.utilization_percent is None or self.window_seconds is None:
                raise ValueError("CPU rate requires a measured interval")
        elif self.utilization_percent is not None or self.window_seconds is not None:
            raise ValueError("unknown CPU rate cannot contain a current value")
        return self


class MemoryObservation(Contract):
    quality: Quality
    total_bytes: int | None = Field(default=None, gt=0)
    available_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_values(self) -> "MemoryObservation":
        if self.quality == "ok":
            if self.total_bytes is None or self.available_bytes is None:
                raise ValueError("memory observation requires both counters")
            if self.available_bytes > self.total_bytes:
                raise ValueError("available memory exceeds total memory")
        elif self.total_bytes is not None or self.available_bytes is not None:
            raise ValueError("unknown memory cannot contain current counters")
        return self


class SystemObservation(TimedObservation):
    source: Literal["linux_procfs"] = "linux_procfs"
    cpu: CpuObservation
    memory: MemoryObservation


class GpuObservation(TimedObservation):
    quality: Quality
    gpus: list[GpuSample] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_values(self) -> "GpuObservation":
        if self.quality != "ok" and self.gpus:
            raise ValueError("unavailable GPU metrics cannot be current")
        if len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate GPU UUID")
        if len({gpu.index for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate GPU index")
        return self


class GpuComputeSummary(Contract):
    gpu_uuid: str = Field(min_length=1, max_length=128)
    reported_processes: int = Field(ge=0)
    reported_memory_mib: int | None = Field(default=None, ge=0)


class ComputeObservation(TimedObservation):
    quality: Literal["ok", "error", "unsupported", "unknown"]
    scope: Literal["driver_reported_compute_apps"] = "driver_reported_compute_apps"
    user_scope: Literal["unknown"] = "unknown"
    reported_processes: int | None = Field(default=None, ge=0)
    gpus: list[GpuComputeSummary] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def valid_values(self) -> "ComputeObservation":
        if self.quality == "ok":
            if self.reported_processes is None:
                raise ValueError("reported process count is missing")
            if any(gpu.reported_processes > self.reported_processes for gpu in self.gpus):
                raise ValueError("GPU process count exceeds the total distinct count")
        elif self.gpus or self.reported_processes is not None:
            raise ValueError("unknown process observation cannot contain current counts")
        if len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate compute GPU UUID")
        return self


class ResourceObservation(TimedObservation):
    schema_version: Literal["aris.resource-observation/v1"] = "aris.resource-observation/v1"
    gpu: GpuObservation
    system: SystemObservation
    compute: ComputeObservation

    @model_validator(mode="after")
    def valid_times(self) -> "ResourceObservation":
        if any(
            part.observed_at > self.observed_at for part in (self.gpu, self.system, self.compute)
        ):
            raise ValueError("component observation follows the combined observation")
        return self


class ResourceInspection(Contract):
    schema_version: Literal["aris.resource-inspection/v1"] = "aris.resource-inspection/v1"
    hostname: str = Field(min_length=1, max_length=256)
    resources: ResourceObservation


class ResourceTelemetryBatch(AgentEnvelope):
    schema_version: Literal["aris.telemetry/v2"] = "aris.telemetry/v2"
    resources: ResourceObservation

    @model_validator(mode="after")
    def consistent_time(self) -> "ResourceTelemetryBatch":
        self.observed_at = self.observed_at.astimezone(UTC)
        self.sent_at = self.sent_at.astimezone(UTC)
        if self.resources.observed_at != self.observed_at:
            raise ValueError("resource time differs from the envelope")
        return self


class NodeResourceStatus(Contract):
    node: NodeStatus
    resource_seq: int | None = None
    system_freshness: Literal["CURRENT", "STALE", "MISSING"]
    system_observed_at: AwareDatetime | None = None
    system: SystemObservation | None = None
    compute_freshness: Literal["CURRENT", "STALE", "MISSING"]
    compute_observed_at: AwareDatetime | None = None
    compute: ComputeObservation | None = None


class ResourceStatus(Contract):
    schema_version: Literal["aris.resources/v2"] = "aris.resources/v2"
    generated_at: AwareDatetime
    nodes: list[NodeResourceStatus]
