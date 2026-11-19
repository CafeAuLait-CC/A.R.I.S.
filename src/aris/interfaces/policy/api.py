from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.policy.service import PolicyDetailInput, PolicyReadSurface
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
)

ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: PolicyReadSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/policy", tags=["policy-source-review"])

    @router.post("/session")
    async def session(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        async for chunk in request.stream():
            if chunk:
                raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        result = await run_in_threadpool(
            service.session, authorization(request), request.state.request_id
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/sources")
    async def sources(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicySourceListQuery)
        result = await run_in_threadpool(
            service.sources, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/detail")
    async def detail(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicyDetailInput)
        result = await run_in_threadpool(
            service.detail, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/compare")
    async def compare(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicySourceComparisonQuery)
        result = await run_in_threadpool(
            service.compare, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=PAGE_HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(name: Literal["policy.js", "policy.css"]) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            headers=PAGE_HEADERS,
            media_type="text/javascript" if name == "policy.js" else "text/css",
        )

    return router
