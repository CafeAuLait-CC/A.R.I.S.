from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.usage.saved_service import SavedUsageQuery, SavedUsageRead, saved_usage
from aris.interfaces.usage.service import UsageHistoryInput, UsageHistorySurface

ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: UsageHistorySurface) -> APIRouter:
    router = APIRouter(prefix="/internal/usage", tags=["recorded-usage"])

    @router.post("/snapshots/query")
    @router.post("/snapshots/read")
    async def snapshots(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError(
                "VALIDATION_ERROR", "Unexpected saved-statistics query parameters", 422
            )
        selection = (
            await bounded_input(request, SavedUsageRead)
            if request.url.path.endswith("/read")
            else await bounded_input(request, SavedUsageQuery)
        )
        result = await run_in_threadpool(
            saved_usage, service, authorization(request), request.state.request_id, selection
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/session")
    async def session(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        async for chunk in request.stream():
            if chunk:
                raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        result = await run_in_threadpool(service.session, authorization(request))
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/query")
    async def query(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected usage query parameters", 422)
        selection = await bounded_input(request, UsageHistoryInput)
        result = await run_in_threadpool(
            service.read, authorization(request), request.state.request_id, selection
        )
        assert not isinstance(result, bytes)
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/export")
    async def export(request: Request) -> Response:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected usage query parameters", 422)
        selection = await bounded_input(request, UsageHistoryInput)
        result = await run_in_threadpool(
            service.read, authorization(request), request.state.request_id, selection, output="csv"
        )
        assert isinstance(result, bytes)
        return Response(
            result,
            media_type="text/csv; charset=utf-8",
            headers={
                **PAGE_HEADERS,
                "Content-Disposition": 'attachment; filename="aris-recorded-usage.csv"',
            },
        )

    @router.post("/report")
    async def report(request: Request) -> Response:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected usage query parameters", 422)
        selection = await bounded_input(request, UsageHistoryInput)
        result = await run_in_threadpool(
            service.read, authorization(request), request.state.request_id, selection, output="html"
        )
        assert isinstance(result, bytes)
        return Response(
            result,
            media_type="text/html; charset=utf-8",
            headers={
                **PAGE_HEADERS,
                "Content-Disposition": 'attachment; filename="aris-recorded-usage-report.html"',
            },
        )

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=PAGE_HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(name: Literal["usage.js", "usage-saved.js", "usage.css"]) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            headers=PAGE_HEADERS,
            media_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    return router
