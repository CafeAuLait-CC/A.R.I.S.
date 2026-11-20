from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from pydantic import Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError, ErrorDetail, ErrorResponse
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.identity.slack_directory import SlackRosterQuery
from aris.interfaces.members.service import GeneralQuery, MemberPortalSurface

COOKIE = "aris_member_session"
COOKIE_PATH = "/internal"
ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


class LoginInput(AccessContract):
    credential: SecretStr = Field(min_length=32, max_length=256, repr=False)
    remember: bool = Field(default=True, strict=True)


class PageInput(AccessContract):
    after: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


def cookie(request: Request) -> str | None:
    headers = request.headers.getlist("cookie")
    if (
        len(headers) > 1
        or sum(
            part.strip().split("=", 1)[0] == COOKIE
            for header in headers
            for part in header.split(";")
        )
        > 1
    ):
        raise DomainError("PERMISSION_DENIED", "Ambiguous session credential", 401)
    return request.cookies.get(COOKIE)


def origin(request: Request, expected: str) -> None:
    if str(request.base_url).rstrip("/") != expected:
        raise DomainError("PERMISSION_DENIED", "This portal origin is not allowed", 403)
    origins = request.headers.getlist("origin")
    if request.method not in ("GET", "HEAD") or origins:
        if origins != [expected]:
            raise DomainError("PERMISSION_DENIED", "A same-origin request is required", 403)
    sites = request.headers.getlist("sec-fetch-site")
    if sites and sites != ["same-origin"] and sites != ["none"]:
        raise DomainError("PERMISSION_DENIED", "Cross-origin requests are not allowed", 403)


def install_session_transport(app: FastAPI, surface: MemberPortalSurface) -> None:
    @app.middleware("http")
    async def member_session(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith("/internal/"):
            return await call_next(request)
        try:
            raw = cookie(request)
            if raw is not None:
                if request.headers.getlist("authorization"):
                    raise DomainError("PERMISSION_DENIED", "Choose one authentication method", 401)
                origin(request, surface.origin)
                if (
                    request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.url.path != "/internal/members/login"
                ):
                    tokens = request.headers.getlist("x-csrf-token")
                    if len(tokens) != 1:
                        raise DomainError("PERMISSION_DENIED", "The page token is required", 403)
                    await run_in_threadpool(surface.login.current, raw, tokens[0])
                request.state.member_authorization = "Bearer " + raw
            elif request.url.path.startswith("/internal/members/") and request.method != "GET":
                origin(request, surface.origin)
            return await call_next(request)
        except DomainError as error:
            return JSONResponse(
                ErrorResponse(
                    error=ErrorDetail(
                        code=error.code,
                        message=error.message,
                        request_id=getattr(request.state, "request_id", str(uuid4())),
                    )
                ).model_dump(mode="json"),
                status_code=error.status,
                headers=PAGE_HEADERS,
            )


def build_router(surface: MemberPortalSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/members", tags=["member-portal"])

    @router.post("/login")
    async def login(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected login query parameters", 422)
        data = await bounded_input(request, LoginInput)
        grant = await run_in_threadpool(
            surface.login.login,
            data.credential,
            remember=data.remember,
            request_id=request.state.request_id,
        )
        if previous := cookie(request):
            await run_in_threadpool(surface.login.logout, previous)
        response = JSONResponse({"signed_in": True}, headers=PAGE_HEADERS)
        response.set_cookie(
            COOKIE,
            grant.token.get_secret_value(),
            max_age=grant.max_age,
            path=COOKIE_PATH,
            secure=urlsplit(surface.origin).scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/session")
    async def session(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected session query parameters", 422)
        raw = cookie(request)
        view = await run_in_threadpool(surface.login.current, raw)
        identity = await run_in_threadpool(surface.members.identity, view.subject)
        return JSONResponse(
            {
                "actor": identity.model_dump(mode="json"),
                "csrf_token": view.csrf.get_secret_value(),
                "expires_at": view.expires_at.isoformat(),
                "nodes": list(surface.node_ids),
                "workspace_namespace": surface.members.administration.namespace.namespace,
                "slack_roster_available": surface.roster_source is not None,
            },
            headers=PAGE_HEADERS,
        )

    @router.post("/logout")
    async def logout(request: Request) -> JSONResponse:
        await bounded_input(request, AccessContract)
        await run_in_threadpool(
            surface.login.logout, cookie(request), request_id=request.state.request_id
        )
        response = JSONResponse({"signed_out": True}, headers=PAGE_HEADERS)
        response.delete_cookie(
            COOKIE,
            path=COOKIE_PATH,
            secure=urlsplit(surface.origin).scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/directory")
    async def directory(request: Request) -> JSONResponse:
        data = await bounded_input(request, PageInput)
        result = await run_in_threadpool(
            surface.read_directory, authorization(request), data.after, data.limit
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/slack-members")
    async def roster(request: Request) -> JSONResponse:
        data = await bounded_input(request, SlackRosterQuery)
        result = await run_in_threadpool(
            surface.roster, authorization(request), request.state.request_id, data
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/general")
    async def general(request: Request) -> JSONResponse:
        data = await bounded_input(request, GeneralQuery)
        result = await run_in_threadpool(
            surface.general, authorization(request), request.state.request_id, data
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=PAGE_HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(name: Literal["members.js", "members.css"]) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            media_type="text/javascript" if name.endswith(".js") else "text/css",
            headers=PAGE_HEADERS,
        )

    return router
