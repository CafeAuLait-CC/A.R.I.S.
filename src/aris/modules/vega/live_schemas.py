"""One ephemeral preview protocol, separate from durable resource reports."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aris.core.contracts import Contract
from aris.modules.vega.disk_schemas import DiskObservation
from aris.modules.vega.resource_schemas import (
    ComputeObservation,
    ResourceObservation,
    SystemObservation,
)
from aris.modules.vega.schemas import GpuSample


class LiveHello(Contract):
    type: Literal["hello"] = "hello"
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    session_id: UUID


class PreviewLease(Contract):
    type: Literal["preview"] = "preview"
    lease_seconds: int = Field(ge=0, le=30)
    interval_seconds: int = Field(default=1, ge=1, le=5)


class LiveFrame(Contract):
    type: Literal["sample"] = "sample"
    session_id: UUID
    seq: int = Field(ge=1, le=9223372036854775807)
    resources: ResourceObservation

    @property
    def observed_at(self) -> AwareDatetime:
        return self.resources.gpu.observed_at

    @property
    def quality(self) -> Literal["ok", "error"]:
        return "ok" if self.resources.gpu.quality == "ok" else "error"

    @property
    def gpus(self) -> list[GpuSample]:
        return self.resources.gpu.gpus


class LiveNode(Contract):
    node_id: str
    agent_id: str
    hostname: str
    connected: bool
    quality: Literal["live", "waiting", "stale", "error", "offline"]
    session_id: UUID
    seq: int | None = None
    observed_at: AwareDatetime | None = None
    gpus: list[GpuSample] = Field(default_factory=list)
    system_freshness: Literal["CURRENT", "STALE", "MISSING"]
    system_observed_at: AwareDatetime | None = None
    system: SystemObservation | None = None
    compute_freshness: Literal["CURRENT", "STALE", "MISSING"]
    compute_observed_at: AwareDatetime | None = None
    compute: ComputeObservation | None = None
    disks_freshness: Literal["CURRENT", "STALE", "MISSING"] = "MISSING"
    disks_observed_at: AwareDatetime | None = None
    disks: DiskObservation | None = None


class LiveSnapshot(Contract):
    generated_at: AwareDatetime
    nodes: list[LiveNode]


class RelayNodeStatus(Contract):
    node_id: str
    session_id: UUID
    connected: bool
    preview_requested: bool
    state: Literal["idle", "waiting", "live", "stale", "error", "offline"]
    last_frame_seq: int | None
    last_frame_observed_at: AwareDatetime | None
    last_frame_age_seconds: float | None
    received_frames: int = Field(ge=0)
    accepted_frames: int = Field(ge=0)
    ignored_inactive_frames: int = Field(ge=0)
    last_input_seq: int | None
    last_input_age_seconds: float | None


class RelayStatus(Contract):
    scope: Literal["api_process"] = "api_process"
    generated_at: AwareDatetime
    active_viewers: int = Field(ge=0)
    viewer_capacity: int = Field(ge=0)
    connected_collectors: int = Field(ge=0)
    nodes: list[RelayNodeStatus]
