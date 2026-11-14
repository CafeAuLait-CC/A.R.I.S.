from typing import Literal

from pydantic import AwareDatetime

from aris.core.contracts import Contract
from aris.modules.vega.schemas import GpuSample


class AgentInspection(Contract):
    schema_version: Literal["aris.agent-inspection/v1"] = "aris.agent-inspection/v1"
    hostname: str
    observed_at: AwareDatetime
    gpus: list[GpuSample]
