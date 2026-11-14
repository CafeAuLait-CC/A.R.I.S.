from typing import Annotated

from fastapi import APIRouter, Header, Request

from aris.core.config import Settings
from aris.core.identity import require_agent, require_read
from aris.modules.vega.schemas import ClusterStatus, Heartbeat, IngestResult, TelemetryBatch
from aris.modules.vega.service import VegaService


def build_router(service: VegaService, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/agent/v1/telemetry/batch", response_model=IngestResult, tags=["agent-v1"])
    def telemetry(
        batch: TelemetryBatch,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, batch.agent_id, batch.node_id, x_agent_token)
        return service.ingest(batch, request.state.request_id)

    @router.post("/api/agent/v1/heartbeat", response_model=IngestResult, tags=["agent-v1"])
    def heartbeat(
        message: Heartbeat,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, message.agent_id, message.node_id, x_agent_token)
        return service.ingest(message, request.state.request_id)

    @router.get("/api/public/v1/resources", response_model=ClusterStatus, tags=["resources-v1"])
    def resources(authorization: Annotated[str | None, Header()] = None) -> ClusterStatus:
        require_read(settings, authorization)
        return service.status()

    return router
