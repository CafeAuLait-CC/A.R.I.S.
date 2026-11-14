from typing import Annotated

from pydantic import Field, TypeAdapter

from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat, TelemetryBatch

AgentReport = Heartbeat | TelemetryBatch | ResourceTelemetryBatch
TelemetryReport = Annotated[
    TelemetryBatch | ResourceTelemetryBatch, Field(discriminator="schema_version")
]
TELEMETRY_REPORT: TypeAdapter[TelemetryBatch | ResourceTelemetryBatch] = TypeAdapter(
    TelemetryReport
)
