from typing import Annotated

from fastapi import APIRouter, Header, Request

from aris.core.config import Settings
from aris.core.identity import require_agent, require_read
from aris.modules.vega.resource_schemas import ResourceStatus, ResourceTelemetryBatch
from aris.modules.vega.schemas import ClusterStatus, Heartbeat, IngestResult, TelemetryBatch
from aris.modules.vega.service import VegaService


def build_router(service: VegaService, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/vega/v1/telemetry/batch", response_model=IngestResult, tags=["vega-collector-v1"]
    )
    def telemetry(
        batch: TelemetryBatch,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, batch.agent_id, batch.node_id, x_agent_token)
        return service.ingest(batch, request.state.request_id)

    @router.post("/vega/v1/heartbeat", response_model=IngestResult, tags=["vega-collector-v1"])
    def heartbeat(
        message: Heartbeat,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, message.agent_id, message.node_id, x_agent_token)
        return service.ingest(message, request.state.request_id)

    @router.post(
        "/vega/v2/telemetry/batch", response_model=IngestResult, tags=["vega-collector-v2"]
    )
    def resource_telemetry(
        batch: ResourceTelemetryBatch,
        request: Request,
        x_agent_token: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        require_agent(settings, batch.agent_id, batch.node_id, x_agent_token)
        return service.ingest(batch, request.state.request_id)

    @router.get("/public/v2/resources", response_model=ResourceStatus, tags=["resources-v2"])
    def component_resources(
        authorization: Annotated[str | None, Header()] = None,
    ) -> ResourceStatus:
        require_read(settings, authorization)
        return service.resource_status()

    @router.get("/public/v1/resources", response_model=ClusterStatus, tags=["resources-v1"])
    def resources(authorization: Annotated[str | None, Header()] = None) -> ClusterStatus:
        require_read(settings, authorization)
        return service.status()

    return router
