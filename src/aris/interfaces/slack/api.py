from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from aris.core.contracts import DomainError
from aris.interfaces.slack.service import SlackService
from aris.interfaces.slack.vram_assets import VRAM_ASSET_ROUTE, asset_file


def build_router(service: SlackService) -> APIRouter:
    router = APIRouter(tags=["slack"])

    def reject_ambiguous_signatures(request: Request) -> None:
        if any(
            len(request.headers.getlist(name)) > 1
            for name in (
                "x-slack-request-timestamp",
                "x-slack-signature",
            )
        ):
            raise DomainError("PERMISSION_DENIED", "Ambiguous Slack authentication", 401)

    @router.get(VRAM_ASSET_ROUTE + "/{filename}", include_in_schema=False)
    def vram_bar(filename: str) -> FileResponse:
        path = asset_file(filename)
        if path is None:
            raise DomainError("NOT_FOUND", "Unknown static asset", 404)
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

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
        reject_ambiguous_signatures(request)
        reply = await run_in_threadpool(
            service.command,
            await read_body(request),
            x_slack_request_timestamp,
            x_slack_signature,
            request.state.request_id,
        )
        return JSONResponse(reply.payload, status_code=reply.status_code)

    @router.post("/slack/events")
    async def events(
        request: Request,
        x_slack_request_timestamp: Annotated[str | None, Header()] = None,
        x_slack_signature: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        reject_ambiguous_signatures(request)
        reply = await run_in_threadpool(
            service.event,
            await read_body(request),
            request.headers.get("content-type", ""),
            x_slack_request_timestamp,
            x_slack_signature,
            request.state.request_id,
        )
        return JSONResponse(
            reply.payload,
            status_code=reply.status_code,
            background=BackgroundTask(reply.after_ack) if reply.after_ack else None,
        )

    return router
