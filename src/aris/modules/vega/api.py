from typing import Annotated

from fastapi import APIRouter, Header, Request

from aris.core.config import Settings
from aris.core.identity import require_agent, require_read
from aris.modules.vega.resource_schemas import ResourceStatus, ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat, IngestResult
from aris.modules.vega.service import VegaService


def build_router(service: VegaService, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/vega/telemetry/batch", response_model=IngestResult, tags=["vega-collector"])
    def telemetry(
        batch: ResourceTelemetryBatch,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, batch.agent_id, batch.node_id, x_agent_token)
        return service.ingest(batch, request.state.request_id)

    @router.post("/vega/heartbeat", response_model=IngestResult, tags=["vega-collector"])
    def heartbeat(
        message: Heartbeat,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, message.agent_id, message.node_id, x_agent_token)
        return service.ingest(message, request.state.request_id)

    @router.get("/public/resources", response_model=ResourceStatus, tags=["resources"])
    def resources(authorization: Annotated[str | None, Header()] = None) -> ResourceStatus:
        require_read(settings, authorization)
        return service.resource_status()

    return router
