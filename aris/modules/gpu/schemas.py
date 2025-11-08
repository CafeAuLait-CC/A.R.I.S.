from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

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
    node: str
    gpu_index: int
    username: str
    process_count: int
    memory_used_mb: int
    timestamp: datetime
