"""Explicit public browser surface, independent of Cloudflare edge rules."""

import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aris.core.contracts import DomainError
from aris.interfaces.members.api import PAGE_HEADERS, origin

PUBLIC_GET_PATHS = frozenset(
    {
        "/",
        "/usage",
        "/account/session",
        "/assets/members.css",
        "/assets/members.js",
        "/assets/usage/usage.css",
        "/assets/usage/usage.js",
        "/assets/usage/usage-saved.js",
        "/assets/usage/usage-weekly.js",
        "/assets/usage/usage-group.js",
        "/assets/usage/usage-activity.js",
        "/assets/usage/usage-hardware.js",
        "/assets/usage/usage-hardware-chart.js",
    }
)
PUBLIC_POST_PATHS = frozenset(
    {
        "/account/login",
        "/account/invitation",
        "/account/register",
        "/account/password-reset",
        "/account/password",
        "/account/logout",
        "/account/directory",
        "/account/slack-members",
        "/account/general",
        "/requests",
        "/usage/session",
        "/usage/query",
        "/usage/export",
        "/usage/report",
        "/usage/activity/query",
        "/usage/groups/query",
        "/usage/groups/report",
        "/usage/groups/csv",
        "/usage/snapshots/query",
        "/usage/snapshots/read",
        "/usage/weekly/plans",
        "/usage/weekly/query",
        "/usage/weekly/read",
        "/usage/weekly/report",
        "/usage/weekly/csv",
    }
)
REQUEST_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def public_path_allowed(method: str, path: str) -> bool:
    if method in {"GET", "HEAD"}:
        return path in PUBLIC_GET_PATHS or bool(
            re.fullmatch(rf"/requests/{REQUEST_UUID}/review", path)
        )
    return method == "POST" and (
        path in PUBLIC_POST_PATHS
        or bool(re.fullmatch(rf"/requests/{REQUEST_UUID}/(?:confirm|cancel)", path))
    )


class PublicPortalBoundary:
    def __init__(self, app: ASGIApp, expected_origin: str, port: int) -> None:
        self.app, self.expected_origin, self.port = app, expected_origin, port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope)
        path, method = request.url.path, request.method
        health = (
            method == "GET"
            and path in {"/health/ready", "/health/live"}
            and request.client is not None
            and request.client.host in {"127.0.0.1", "::1"}
            and request.headers.getlist("host") == [f"127.0.0.1:{self.port}"]
            and request.url.scheme == "http"
        )
        if health:
            await self.app(scope, receive, send)
            return
        try:
            origin(request, self.expected_origin)
            if (
                len(request.headers.getlist("host")) != 1
                or not public_path_allowed(method, path)
                or scope.get("raw_path", path.encode()) != path.encode()
            ):
                raise DomainError("NOT_FOUND", "Page not found", 404)
        except DomainError as error:
            response = JSONResponse(
                {"error": {"code": error.code, "message": error.message}},
                status_code=error.status,
                headers=PAGE_HEADERS,
            )
            await response(scope, receive, send)
            return
        # HEAD has the same authentication and headers as GET, without its response body.
        forwarded = dict(scope, method="GET") if method == "HEAD" else scope

        async def private_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message["headers"]
                    if key.lower() != b"cache-control"
                ]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            elif method == "HEAD" and message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app(forwarded, receive, private_response)
