from __future__ import annotations

from typing import List, Optional
from datetime import datetime

from pydantic import BaseModel, Field


# ========= Agent → ARIS Protocal =========


class AgentGpuInfo(BaseModel):
    uuid: str
    index: int
    name: Optional[str] = None
    total_memory_mb: Optional[int] = None


class AgentRegisterRequest(BaseModel):
    hostname: str = Field(..., description="GPU node hostname")
    agent_version: str | None = Field(None, description="GPU agent version")
    gpus: List[AgentGpuInfo]
    ts: Optional[datetime] = None


class AgentSessionStartRequest(BaseModel):
    hostname: str
    agent_version: str | None = Field(None, description="GPU agent version")
    gpu_uuid: str
    user: str  # gpu_node_username
    pids: List[int] = Field(default_factory=list)
    started_at: datetime


class AgentSessionHeartbeatItem(BaseModel):
    gpu_uuid: str
    user: str
    pids: list[int] = Field(default_factory=list)
    ts: datetime


class AgentSessionHeartbeatRequest(BaseModel):
    hostname: str
    items: list[AgentSessionHeartbeatItem]


class AgentSessionEndRequest(BaseModel):
    hostname: str
    gpu_uuid: str
    user: str
    ended_at: datetime
    started_at: Optional[datetime] = None


class AgentSessionResponse(BaseModel):
    ok: bool = True
    session_id: Optional[str] = None
    detail: Optional[str] = None


# ========= Lagecy (To Explore) =========


class GpuProcessReport(BaseModel):
    gpu_index: int
    username: str
    process_count: int
    memory_used_mb: int


class GpuReport(BaseModel):
    node_name: str
    token: str
    processes: List[GpuProcessReport]
    timestamp: datetime


class GpuStatusView(BaseModel):
    hostname: str
    gpu_index: int
    username: str
    process_count: int
    memory_used_mb: int
    timestamp: datetime
