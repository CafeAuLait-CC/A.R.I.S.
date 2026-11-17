from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from aris.core.contracts import Contract


class GpuTargetQuery(Contract):
    gpu_uuid: str = Field(min_length=1, max_length=128)


class GpuInventoryLabel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gpu_uuid: str
    node_id: str
    index: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=256)


class GpuLabelQuery(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gpu_uuids: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_unique(self) -> "GpuLabelQuery":
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids) or any(
            not 1 <= len(value) <= 128 for value in self.gpu_uuids
        ):
            raise ValueError("GPU labels require bounded unique targets")
        return self


class GpuTargetFact(Contract):
    """Durable target identity and freshness; no reservation decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: AwareDatetime
    gpu_uuid: str
    node_id: str
    present: bool
    node_status: Literal["ONLINE", "STALE", "OFFLINE", "UNKNOWN"]
    telemetry_status: Literal["CURRENT", "STALE", "MISSING", "ERROR"]
    observed_at: AwareDatetime | None
    received_at: AwareDatetime | None
    last_seen_at: AwareDatetime
    last_received_at: AwareDatetime
    telemetry_seq: int
