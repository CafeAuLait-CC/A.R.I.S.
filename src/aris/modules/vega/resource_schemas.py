"""Component observations with independent quality and timestamps."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import Contract
from aris.modules.vega.schemas import GpuSample

Quality = Literal["ok", "error", "unsupported"]


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


class SystemObservation(Contract):
    observed_at: AwareDatetime
    source: Literal["linux_procfs"] = "linux_procfs"
    cpu: CpuObservation
    memory: MemoryObservation


class GpuObservation(Contract):
    observed_at: AwareDatetime
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


class ComputeObservation(Contract):
    observed_at: AwareDatetime
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


class ResourceObservation(Contract):
    schema_version: Literal["aris.resource-observation/v1"] = "aris.resource-observation/v1"
    observed_at: AwareDatetime
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
