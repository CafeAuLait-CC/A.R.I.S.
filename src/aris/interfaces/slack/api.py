from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from aris.core.contracts import DomainError
from aris.interfaces.slack.service import SlackService


def build_router(service: SlackService) -> APIRouter:
    router = APIRouter(tags=["slack"])

    async def read_body(request: Request) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > service.config.max_body_bytes:
                raise DomainError("PAYLOAD_TOO_LARGE", "Slack request exceeds the body limit", 413)
        return bytes(body)

    @router.post("/slack/commands")
    async def commands(
        request: Request,
        x_slack_request_timestamp: Annotated[str | None, Header()] = None,
        x_slack_signature: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        reply = await run_in_threadpool(
            service.command, await read_body(request), x_slack_request_timestamp, x_slack_signature
        )
        return JSONResponse(reply.payload, status_code=reply.status_code)

    @router.post("/slack/events")
    async def events(
        request: Request,
        x_slack_request_timestamp: Annotated[str | None, Header()] = None,
        x_slack_signature: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        reply = await run_in_threadpool(
            service.event,
            await read_body(request),
            request.headers.get("content-type", ""),
            x_slack_request_timestamp,
            x_slack_signature,
            request.state.request_id,
        )
        return JSONResponse(reply.payload, status_code=reply.status_code)

    return router
