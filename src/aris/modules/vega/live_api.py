import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.identity import require_agent, require_read
from aris.modules.vega.live import CollectorConnection, LiveHub, Viewer
from aris.modules.vega.live_schemas import (
    LiveFrame,
    LiveHello,
    RelayStatus,
    ResourceLiveFrame,
    ResourceLiveHello,
)

logger = logging.getLogger(__name__)


async def preview_events(
    hub: LiveHub, request: Request, key: UUID, viewer: Viewer, resource_mode: bool = False
) -> AsyncIterator[str]:
    try:
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            if not hub.renew(key, viewer):
                return
            snapshot = hub.resource_snapshot() if resource_mode else hub.snapshot()
            yield "event: preview\ndata: " + snapshot.model_dump_json() + "\n\n"
            try:
                await asyncio.wait_for(viewer.changed.get(), timeout=1)
            except TimeoutError:
                pass
    finally:
        hub.close_viewer(key)


async def send_controls(socket: WebSocket, hub: LiveHub, connection: CollectorConnection) -> None:
    while connection.connected:
        await socket.send_text(hub.lease(connection).model_dump_json())
        try:
            await asyncio.wait_for(connection.control.get(), timeout=10)
        except TimeoutError:
            pass
    await socket.close(code=1000)


def build_live_router(hub: LiveHub, settings: Settings) -> APIRouter:
    router = APIRouter()

    async def collector_connection(socket: WebSocket, resource_mode: bool) -> None:
        # Authenticate before upgrading; identities also bind the versioned hello.
        agent_id = socket.query_params.get("agent_id", "")
        node_id = socket.query_params.get("node_id", "")
        try:
            require_agent(settings, agent_id, node_id, socket.headers.get("x-agent-token"))
        except DomainError:
            await socket.close(code=1008)
            return
        await socket.accept()
        connection: CollectorConnection | None = None
        sender: asyncio.Task[None] | None = None
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(raw.encode()) > 2048:
                raise ValueError("preview hello too large")
            hello = (ResourceLiveHello if resource_mode else LiveHello).model_validate_json(raw)
            if hello.agent_id != agent_id or hello.node_id != node_id:
                raise ValueError("preview identity mismatch")
            connection = hub.attach(hello)
            sender = asyncio.create_task(send_controls(socket, hub, connection))
            while connection.connected:
                raw = await socket.receive_text()
                if len(raw.encode()) > 65536:
                    raise ValueError("preview frame too large")
                frame = (ResourceLiveFrame if resource_mode else LiveFrame).model_validate_json(raw)
                hub.accept(connection, frame)
        except WebSocketDisconnect:
            pass
        except (ValueError, TimeoutError, RuntimeError, KeyError) as exc:
            logger.warning("preview_connection failure_type=%s", type(exc).__name__)
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=1008)
        finally:
            if connection:
                hub.detach(connection)
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
                    await sender

    @router.websocket("/vega/v1/live")
    async def collector_live(socket: WebSocket) -> None:
        await collector_connection(socket, False)

    @router.websocket("/vega/v2/live")
    async def collector_resources(socket: WebSocket) -> None:
        await collector_connection(socket, True)

    async def subscription(request: Request, resource_mode: bool) -> StreamingResponse:
        require_read(settings, request.headers.get("authorization"))
        key, viewer = hub.open_viewer()
        return StreamingResponse(
            preview_events(hub, request, key, viewer, resource_mode),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/internal/v1/live", tags=["live-preview-v1"])
    async def subscribe(request: Request) -> StreamingResponse:
        return await subscription(request, False)

    @router.get("/internal/v2/live", tags=["live-preview-v2"])
    async def subscribe_resources(request: Request) -> StreamingResponse:
        return await subscription(request, True)

    @router.get("/internal/v1/live/status", tags=["live-preview-status"])
    async def relay_status(request: Request) -> RelayStatus:
        require_read(settings, request.headers.get("authorization"))
        return hub.relay_status()

    return router
