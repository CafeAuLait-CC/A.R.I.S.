from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import Contract

Capability = Literal["node.resources", "heartbeat"]


class GpuSample(Contract):
    gpu_uuid: str = Field(min_length=1, max_length=128)
    index: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=256)
    memory_total_mib: int = Field(gt=0)
    memory_used_mib: int = Field(ge=0)
    utilization_percent: int = Field(ge=0, le=100)
    temperature_c: int | None = Field(default=None, ge=-50, le=150)

    @model_validator(mode="after")
    def validate_memory(self) -> "GpuSample":
        if self.memory_used_mib > self.memory_total_mib:
            raise ValueError("used memory exceeds total memory")
        return self


class AgentEnvelope(Contract):
    message_id: UUID
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    agent_version: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=1, le=9223372036854775807)
    sent_at: AwareDatetime
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_times(self) -> "AgentEnvelope":
        if self.observed_at > self.sent_at:
            raise ValueError("observation must not follow send time")
        return self


class Heartbeat(AgentEnvelope):
    type: Literal["heartbeat"] = "heartbeat"
    collector_status: Literal["ok", "error"]
    capabilities: list[Capability] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_capabilities(self) -> "Heartbeat":
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("duplicate capability")
        return self


class IngestResult(Contract):
    ok: Literal[True] = True
    message_id: UUID
    duplicate: bool
    applied_to_current: bool


class GpuStatus(GpuSample):
    present: bool
    observed_state: Literal["idle", "busy", "unknown"]
    availability: Literal["not_evaluated"] = "not_evaluated"


class NodeStatus(Contract):
    node_id: str
    hostname: str
    agent_id: str
    agent_version: str
    status: Literal["ONLINE", "STALE", "OFFLINE"]
    observed_at: AwareDatetime | None
    received_at: AwareDatetime | None
    last_seen_at: AwareDatetime
    last_received_at: AwareDatetime
    heartbeat_at: AwareDatetime | None
    heartbeat_received_at: AwareDatetime | None
    collector_status: Literal["ok", "error", "unknown"]
    telemetry_status: Literal["CURRENT", "STALE", "MISSING", "ERROR"]
    seq: int
    gpus: list[GpuStatus]


class ClusterStatus(Contract):
    generated_at: AwareDatetime
    nodes: list[NodeStatus]
