from typing import Literal

from pydantic import AwareDatetime, Field

from aris.core.contracts import Contract


class GpuTargetQuery(Contract):
    gpu_uuid: str = Field(min_length=1, max_length=128)


class GpuTargetFact(Contract):
    """Durable target identity and freshness; no reservation decision."""

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
