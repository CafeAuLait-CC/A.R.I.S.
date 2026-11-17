import argparse
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from aris import __version__
from aris.apps.composition.m2 import (
    M2RuntimeConfig,
    build_m2_components,
    load_m2_config,
    validate_m2_configuration,
)
from aris.core.config import Settings
from aris.core.contracts import DomainError, ErrorDetail, ErrorResponse
from aris.core.db import SCHEMA_REVISION, build_engine, check_database, session_factory
from aris.interfaces.commands.api import build_router as build_command_router
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.identity.api import build_router as build_identity_router
from aris.interfaces.identity.service import IdentityAdminSurface
from aris.interfaces.notifications.api import build_router as build_notification_router
from aris.interfaces.notifications.service import NotificationAdminSurface
from aris.interfaces.reservations.api import build_router as build_reservation_router
from aris.interfaces.reservations.service import UserReservationSurface
from aris.interfaces.slack.api import build_router as build_slack_router
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.home_reservations import HomeReservationSource
from aris.interfaces.slack.reservation_drafts import SlackReservationDrafts
from aris.interfaces.slack.reservations import SlackReservationQueries
from aris.interfaces.slack.service import SlackService
from aris.interfaces.slack.user_commands import SlackCommandAdapter
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
    slack_commands: SlackCommandAdapter | None = None,
    user_reservations: UserReservationSurface | None = None,
    slack_reservations: SlackReservationQueries | None = None,
    slack_drafts: SlackReservationDrafts | None = None,
    home_reservations: HomeReservationSource | None = None,
    identity_admin: IdentityAdminSurface | None = None,
    notification_admin: NotificationAdminSurface | None = None,
    m2_config: M2RuntimeConfig | None = None,
) -> FastAPI:
    config = settings or Settings()
    slack_config = slack_settings or SlackSettings()
    if m2_config is not None:
        if any(
            surface is not None
            for surface in (
                user_commands,
                slack_commands,
                user_reservations,
                slack_reservations,
                slack_drafts,
                home_reservations,
                identity_admin,
                notification_admin,
            )
        ):
            raise ValueError("M2 configuration and explicit command surfaces cannot be combined")
        validate_m2_configuration(config, slack_config, m2_config)
    if slack_commands is not None:
        slack_commands.validate_configuration(slack_config)
    if slack_reservations is not None:
        slack_reservations.validate_configuration(slack_config)
    if slack_drafts is not None:
        slack_drafts.validate_configuration(slack_config)
    if home_reservations is not None:
        home_reservations.validate_configuration(slack_config)
    if identity_admin is not None:
        if user_commands is None or identity_admin.commands is not user_commands:
            raise ValueError("identity UI requires the same authenticated command surface")
        identity_admin.validate_workspace(slack_config)
    if user_commands is not None:
        user_commands.validate_configuration(
            config.environment,
            [
                config.read_token,
                *(credential.token for credential in config.agent_credentials.values()),
            ],
        )
    if notification_admin is not None:
        if user_commands is None or notification_admin.commands is not user_commands:
            raise ValueError("notification UI requires the same authenticated command surface")
        notification_admin.validate_workspace(slack_config)
    if user_reservations is not None:
        user_reservations.validate_configuration(
            config.environment,
            [
                config.read_token,
                *(credential.token for credential in config.agent_credentials.values()),
                slack_config.bot_token,
                slack_config.signing_secret,
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
    components = None
    if m2_config is not None:
        try:
            check_database(engine)
            components = build_m2_components(sessions, config, slack_config, m2_config)
        except Exception:
            engine.dispose()
            raise
        user_commands, slack_commands = components.user_api, components.slack_callbacks
        user_reservations = components.user_reservations
        slack_reservations = components.slack_reservations
        slack_drafts = components.slack_drafts
        home_reservations = components.home_reservations
        identity_admin = components.identity_admin
        notification_admin = components.notification_admin
    app.state.m2 = components
    vega = VegaService(sessions, config)
    app.include_router(build_router(vega, config))
    app.state.live_hub = LiveHub()
    app.include_router(build_live_router(app.state.live_hub, config))
    app.include_router(
        build_slack_router(
            SlackService(
                sessions,
                slack_config,
                vega,
                command_adapter=slack_commands,
                reservation_queries=slack_reservations,
                reservation_drafts=slack_drafts,
                home_reservations=home_reservations,
            )
        )
    )
    if user_commands is not None:
        app.include_router(build_command_router(user_commands))
    if user_reservations is not None:
        app.include_router(build_reservation_router(user_reservations))
    if identity_admin is not None:
        app.include_router(build_identity_router(identity_admin))
    if notification_admin is not None:
        app.include_router(build_notification_router(notification_admin))

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
        private_query = user_reservations is not None and (
            request.url.path == "/internal/reservations"
            or request.url.path.startswith("/internal/reservations/")
        )
        private_command = user_commands is not None and (
            request.url.path == "/internal/commands"
            or request.url.path.startswith("/internal/commands/")
        )
        private_identity = identity_admin is not None and (
            request.url.path == "/internal/identity"
            or request.url.path.startswith("/internal/identity/")
        )
        private_notification = notification_admin is not None and (
            request.url.path == "/internal/notifications"
            or request.url.path.startswith("/internal/notifications/")
        )
        if (
            private_query
            or private_command
            or private_identity
            or private_notification
            or request.url.path == "/slack/commands"
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

    parser = argparse.ArgumentParser(
        description="Run the API; M2 requires explicit private configuration"
    )
    parser.add_argument("--m2-config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not args.host or any(char.isspace() for char in args.host) or "/" in args.host:
        parser.error("host must be a bind address or hostname")
    if args.m2_config is not None:
        uvicorn.run(
            create_app(m2_config=load_m2_config(args.m2_config)),
            host=args.host,
            port=args.port,
            ws_max_size=65536,
            ws_max_queue=2,
        )
        return
    uvicorn.run(
        "aris.apps.api.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        ws_max_size=65536,
        ws_max_queue=2,
    )


if __name__ == "__main__":
    main()
