from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from aris.core.contracts import Contract


class GpuTargetQuery(Contract):
    gpu_uuid: str = Field(min_length=1, max_length=128)


class GpuInventoryLabel(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gpu_uuid: str
    node_id: str
    index: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=256)


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
