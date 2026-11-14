import argparse
import asyncio
import html
import ipaddress
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from aris.core.config import Settings
from aris.core.db import build_engine, session_factory
from aris.modules.vega.service import VegaService

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("static")


async def resource_events(
    service: VegaService, disconnected: Callable[[], Awaitable[bool]], interval: float = 3
) -> AsyncIterator[str]:
    yield "retry: 3000\n\n"
    while not await disconnected():
        try:
            snapshot = await run_in_threadpool(service.status)
            yield "event: resources\ndata: " + snapshot.model_dump_json() + "\n\n"
        except Exception as exc:
            logger.warning("dashboard_snapshot failure_type=%s", type(exc).__name__)
            yield (
                "event: dependency_error\ndata: "
                + json.dumps({"code": "DEPENDENCY_UNAVAILABLE"})
                + "\n\n"
            )
        await asyncio.sleep(interval)


def create_dashboard(
    settings: Settings | None = None, interval: float = 3, time_zone: str = "UTC"
) -> FastAPI:
    if not 1 <= interval <= 30:
        raise ValueError("dashboard interval must be between 1 and 30 seconds")
    try:
        ZoneInfo(time_zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid dashboard time zone") from exc
    config = settings or Settings()
    engine = build_engine(config.database_url)
    service = VegaService(session_factory(engine), config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Vega Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.engine = engine
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_access(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = False
        origin = request.headers.get("origin")
        try:
            parsed_origin = urlsplit(origin) if origin else None
            same_origin = parsed_origin is None or (
                parsed_origin.netloc.lower() == request.headers.get("host", "").lower()
                and parsed_origin.scheme in {"http", "https"}
            )
        except ValueError:
            same_origin = False
        if not local or not same_origin:
            return JSONResponse({"error": "LOCAL_ACCESS_ONLY"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index() -> HTMLResponse:
        document = (
            (ASSETS / "index.html")
            .read_text()
            .replace('data-time-zone="UTC"', 'data-time-zone="' + html.escape(time_zone) + '"')
            .replace('data-refresh-seconds="3"', f'data-refresh-seconds="{interval}"')
        )
        return HTMLResponse(document)

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            resource_events(service, request.is_disconnected, interval),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Vega local resource dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--time-zone", default="UTC", help="IANA display time zone")
    args = parser.parse_args()
    uvicorn.run(
        create_dashboard(time_zone=args.time_zone),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
