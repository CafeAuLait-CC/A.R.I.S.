from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat

AgentReport = Heartbeat | ResourceTelemetryBatch
