from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.core.notifications.query_schemas import UncertainNotificationsQuery
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.notifications.service import NotificationAdminSurface

ASSETS = Path(__file__).with_name("static")
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: NotificationAdminSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/notifications", tags=["notification-administration"])

    @router.post("/uncertain/query")
    async def query(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected notification query parameters", 422)
        data = await bounded_input(request, UncertainNotificationsQuery)
        result = await run_in_threadpool(
            service.page, authorization(request), request.state.request_id, data
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(name: Literal["admin.css", "admin.js"]) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            headers=HEADERS,
            media_type="text/javascript" if name == "admin.js" else "text/css",
        )

    @router.get("/{notification_id}")
    async def detail(notification_id: UUID, request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected notification detail parameters", 422)
        result = await run_in_threadpool(
            service.detail, authorization(request), request.state.request_id, notification_id
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return router
