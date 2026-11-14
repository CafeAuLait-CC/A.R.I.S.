"""Ephemeral preview protocol; deliberately separate from durable agent messages."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import Contract
from aris.modules.vega.schemas import GpuSample


class LiveHello(Contract):
    schema_version: Literal["aris.live/v1"] = "aris.live/v1"
    type: Literal["hello"] = "hello"
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=256)
    session_id: UUID


class PreviewLease(Contract):
    schema_version: Literal["aris.live/v1"] = "aris.live/v1"
    type: Literal["preview"] = "preview"
    lease_seconds: int = Field(ge=0, le=30)
    interval_seconds: int = Field(default=1, ge=1, le=5)


class LiveFrame(Contract):
    schema_version: Literal["aris.live/v1"] = "aris.live/v1"
    type: Literal["sample"] = "sample"
    session_id: UUID
    seq: int = Field(ge=1, le=9223372036854775807)
    observed_at: AwareDatetime
    quality: Literal["ok", "error"]
    gpus: list[GpuSample] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def snapshot(self) -> "LiveFrame":
        if self.quality == "error" and self.gpus:
            raise ValueError("failed collection cannot contain current metrics")
        if len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate GPU UUID")
        if len({gpu.index for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("duplicate GPU index")
        return self


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


class LiveSnapshot(Contract):
    schema_version: Literal["aris.live-preview/v1"] = "aris.live-preview/v1"
    generated_at: AwareDatetime
    nodes: list[LiveNode]
