from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.identity.service import DirectoryPageInput, IdentityAdminSurface
from aris.interfaces.identity.slack_directory import SlackRosterQuery

ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: IdentityAdminSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/identity", tags=["identity-administration"])

    @router.post("/directory/query")
    async def directory(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected identity query parameters", 422)
        query = await bounded_input(request, DirectoryPageInput)
        page = await run_in_threadpool(
            service.read_directory, authorization(request), request.state.request_id, query
        )
        return JSONResponse(page.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.post("/slack-members/query")
    async def roster(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected identity query parameters", 422)
        query = await bounded_input(request, SlackRosterQuery)
        page = await run_in_threadpool(
            service.roster, authorization(request), request.state.request_id, query
        )
        return JSONResponse(page.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=PAGE_HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(name: Literal["admin.js", "admin.css"]) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            headers=PAGE_HEADERS,
            media_type="text/javascript" if name == "admin.js" else "text/css",
        )

    return router
