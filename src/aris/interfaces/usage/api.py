from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.usage.activity_service import ActivityPreviewInput, activity_usage
from aris.interfaces.usage.group_service import GroupUsageInput, group_usage
from aris.interfaces.usage.saved_service import SavedUsageQuery, SavedUsageRead, saved_usage
from aris.interfaces.usage.service import UsageHistoryInput, UsageHistorySurface
from aris.interfaces.usage.weekly_service import (
    CombinedWeeklyUsageRead,
    WeeklyPlanRead,
    WeeklyReportRead,
    WeeklyUsageQuery,
    weekly_plans,
    weekly_usage,
)
from aris.interfaces.web_paths import INTERNAL_PATHS, BrowserPaths
from aris.interfaces.workbench import page_shell, script, stylesheet

ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: UsageHistorySurface, paths: BrowserPaths = INTERNAL_PATHS) -> APIRouter:
    router = APIRouter(tags=["recorded-usage"])

    if service.activity is not None:

        @router.post(paths.usage + "/activity/query")
        async def activity(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected activity query parameters", 422)
            selection = await bounded_input(request, ActivityPreviewInput)
            result = await run_in_threadpool(
                activity_usage, service, authorization(request), request.state.request_id, selection
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/groups/query")
    @router.post(paths.usage + "/groups/report")
    @router.post(paths.usage + "/groups/csv")
    async def groups(request: Request) -> Response:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected selected-node query parameters", 422)
        selection = await bounded_input(request, GroupUsageInput)
        route = request.url.path.rsplit("/", 1)[-1]
        output: Literal["page", "html", "csv"] = (
            "page" if route == "query" else "html" if route == "report" else "csv"
        )
        result = await run_in_threadpool(
            group_usage,
            service,
            authorization(request),
            request.state.request_id,
            selection,
            output=output,
        )
        if isinstance(result, bytes):
            extension = "html" if output == "html" else "csv"
            filename = f"aris-selected-node-summary.{extension}"
            return Response(
                result,
                media_type="text/html; charset=utf-8"
                if output == "html"
                else "text/csv; charset=utf-8",
                headers={
                    **PAGE_HEADERS,
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/weekly/plans")
    async def plans(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected weekly plan query parameters", 422)
        selection = await bounded_input(request, WeeklyPlanRead)
        result = await run_in_threadpool(
            weekly_plans, service, authorization(request), request.state.request_id, selection
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/weekly/query")
    @router.post(paths.usage + "/weekly/read")
    @router.post(paths.usage + "/weekly/report")
    @router.post(paths.usage + "/weekly/csv")
    async def weekly(request: Request) -> Response:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected weekly report query parameters", 422)
        route = request.url.path.rsplit("/", 1)[-1]
        selection = (
            await bounded_input(request, WeeklyUsageQuery)
            if route == "query"
            else (await bounded_input(request, WeeklyReportRead)).root
        )
        output: Literal["html", "csv", "json"] = (
            "html" if route == "report" else "csv" if route == "csv" else "json"
        )
        result = await run_in_threadpool(
            weekly_usage,
            service,
            authorization(request),
            request.state.request_id,
            selection,
            output=output,
        )
        if isinstance(result, bytes):
            extension = "html" if output == "html" else "csv"
            filename = (
                f"aris-combined-week-{selection.week}.{extension}"
                if isinstance(selection, CombinedWeeklyUsageRead)
                else f"aris-weekly-usage.{extension}"
            )
            return Response(
                result,
                media_type="text/html; charset=utf-8"
                if output == "html"
                else "text/csv; charset=utf-8",
                headers={
                    **PAGE_HEADERS,
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/snapshots/query")
    @router.post(paths.usage + "/snapshots/read")
    async def snapshots(request: Request) -> Response:
        if request.query_params:
            raise DomainError(
                "VALIDATION_ERROR", "Unexpected saved-statistics query parameters", 422
            )
        route = request.url.path.rsplit("/", 1)[-1]
        selection = (
            await bounded_input(request, SavedUsageRead)
            if route != "query"
            else await bounded_input(request, SavedUsageQuery)
        )
        accept = request.headers.get("accept", "application/json").split(";", 1)[0].strip()
        output: Literal["json", "html", "csv"] = (
            "html"
            if route == "read" and accept == "text/html"
            else "csv"
            if route == "read" and accept == "text/csv"
            else "json"
        )
        result = await run_in_threadpool(
            saved_usage,
            service,
            authorization(request),
            request.state.request_id,
            selection,
            output=output,
        )
        if isinstance(result, bytes):
            extension = "html" if output == "html" else "csv"
            return Response(
                result,
                media_type="text/html; charset=utf-8"
                if output == "html"
                else "text/csv; charset=utf-8",
                headers={
                    **PAGE_HEADERS,
                    "Content-Disposition": f'attachment; filename="aris-saved-usage.{extension}"',
                    "Vary": "Accept",
                },
            )
        return JSONResponse(
            result.model_dump(mode="json"), headers={**PAGE_HEADERS, "Vary": "Accept"}
        )

    @router.post(paths.usage + "/session")
    async def session(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        async for chunk in request.stream():
            if chunk:
                raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        result = await run_in_threadpool(service.session, authorization(request))
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/query")
    async def query(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected usage query parameters", 422)
        selection = await bounded_input(request, UsageHistoryInput)
        result = await run_in_threadpool(
            service.read, authorization(request), request.state.request_id, selection
        )
        assert not isinstance(result, bytes)
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post(paths.usage + "/export")
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

    @router.post(paths.usage + "/report")
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

    @router.get(paths.usage_page, response_class=HTMLResponse)
    def page() -> HTMLResponse:
        html = (ASSETS / "index.html").read_text()
        browser_sessions = getattr(service.authenticator, "browser_sessions", False)
        if browser_sessions:
            html = html.replace("<html ", '<html data-member-sessions="true" ', 1)
        return HTMLResponse(
            paths.render(page_shell(html, enabled=browser_sessions)), headers=PAGE_HEADERS
        )

    @router.get(paths.usage_assets + "/{name}")
    def asset(
        name: Literal[
            "usage.js",
            "usage-saved.js",
            "usage-weekly.js",
            "usage-group.js",
            "usage.css",
            "usage-activity.js",
            "usage-hardware.js",
            "usage-hardware-chart.js",
        ],
    ) -> Response:
        source = (ASSETS / name).read_text()
        if name == "usage.css":
            source = stylesheet() + "\n" + source
        elif name == "usage.js":
            source = script() + "\n" + source
        return Response(
            paths.render(source),
            headers=PAGE_HEADERS,
            media_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    return router
