import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from aris import __version__
from aris.core.config import Settings
from aris.core.contracts import DomainError, ErrorDetail, ErrorResponse
from aris.core.db import SCHEMA_REVISION, build_engine, check_database, session_factory
from aris.interfaces.commands.api import build_router as build_command_router
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.slack.api import build_router as build_slack_router
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.service import SlackService
from aris.modules.vega.api import build_router
from aris.modules.vega.live import LiveHub
from aris.modules.vega.live_api import build_live_router
from aris.modules.vega.service import VegaService

logger = logging.getLogger(__name__)


def error_response(request: Request, code: str, message: str, status: int) -> JSONResponse:
    error = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", str(uuid4())),
        )
    )
    return JSONResponse(
        error.model_dump(), status_code=status, headers={"X-Request-ID": error.error.request_id}
    )


def create_app(
    settings: Settings | None = None,
    slack_settings: SlackSettings | None = None,
    *,
    user_commands: UserCommandSurface | None = None,
) -> FastAPI:
    config = settings or Settings()
    if user_commands is not None:
        user_commands.validate_configuration(
            config.environment,
            [
                config.read_token,
                *(credential.token for credential in config.agent_credentials.values()),
            ],
        )
    engine = build_engine(config.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="A.R.I.S.", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    sessions = session_factory(engine)
    vega = VegaService(sessions, config)
    app.include_router(build_router(vega, config))
    app.state.live_hub = LiveHub()
    app.include_router(build_live_router(app.state.live_hub, config))
    app.include_router(
        build_slack_router(SlackService(sessions, slack_settings or SlackSettings(), vega))
    )
    if user_commands is not None:
        app.include_router(build_command_router(user_commands))

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied if re.fullmatch(r"[\w.-]{1,128}", supplied, re.ASCII) else str(uuid4())
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if user_commands is not None and (
            request.url.path == "/internal/commands"
            or request.url.path.startswith("/internal/commands/")
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        return error_response(request, "VALIDATION_ERROR", "Invalid request schema", 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, _: SQLAlchemyError) -> JSONResponse:
        return error_response(request, "DEPENDENCY_UNAVAILABLE", "Database operation failed", 503)

    @app.exception_handler(HTTPException)
    async def protocol_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(request, "HTTP_ERROR", "Request cannot be served", exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request_id=%s failure_type=%s", request.state.request_id, type(exc).__name__)
        return error_response(request, "INTERNAL_ERROR", "Unexpected error", 500)

    @app.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        try:
            check_database(engine)
        except (SQLAlchemyError, RuntimeError) as exc:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Database migration or access required", 503
            ) from exc
        return {"status": "ready", "schema_revision": SCHEMA_REVISION}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "aris.apps.api.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        ws_max_size=65536,
        ws_max_queue=2,
    )


if __name__ == "__main__":
    main()
