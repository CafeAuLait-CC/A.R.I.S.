"""Manual loopback listener with optional public browser routes; no startup writes."""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from pydantic import Field, SecretStr, ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp

from aris.apps.api.main import create_app
from aris.apps.composition.members import (
    MemberRuntimeConfig,
    build_member_components,
    validate_member_configuration,
)
from aris.apps.ops.identity import read_private
from aris.apps.ops.identity_ui import LocalSlackSettings
from aris.core.access.schemas import AccessContract
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.interfaces.members.live import dashboard_origin
from aris.interfaces.members.public import PublicPortalBoundary
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.web_paths import INTERNAL_PATHS, PUBLIC_PATHS


class LocalMemberConfig(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    read_token: SecretStr = Field(min_length=32, repr=False)
    members: MemberRuntimeConfig
    slack: LocalSlackSettings | None = None

    def settings(self) -> Settings:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"} or not url.database:
            raise ValueError("member portal requires an existing persistent database")
        return Settings(
            _env_file=None,
            environment=self.environment,
            database_url=self.database_url.get_secret_value(),
            read_token=self.read_token,
            agent_credentials={},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=53387)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--vega-dashboard-origin", help="Optional loopback Vega dashboard origin")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or (args.once and not args.worker):
        parser.error("use an unprivileged port; --once requires --worker")
    engine = None
    try:
        config = read_private(args.config, LocalMemberConfig)
        public = urlsplit(config.members.origin).scheme == "https"
        if not public and config.members.origin != f"http://127.0.0.1:{args.port}":
            raise ValueError("local member origin must match the loopback listener")
        settings = config.settings()
        slack = (
            config.slack.settings()
            if config.slack
            else SlackSettings(_env_file=None, team_id="", signing_secret="", bot_token="")
        )
        validate_member_configuration(settings, slack, config.members)
        preview = (
            dashboard_origin(args.vega_dashboard_origin)
            if args.vega_dashboard_origin is not None
            else None
        )
        if preview == f"http://127.0.0.1:{args.port}":
            raise ValueError("Vega dashboard must use a separate listener")
        if args.validate:
            print(
                json.dumps(
                    {
                        "result": "valid",
                        "database_accessed": False,
                        "host": "127.0.0.1",
                        "port": args.port,
                        "commands": [
                            "identity.manage_members",
                            "identity.edit_own_profile",
                        ],
                        "autostart": False,
                        "cookie_authentication": True,
                        "public_browser_routes": public,
                        "live_preview_configured": preview is not None,
                    }
                )
            )
            return 0
        url = make_url(settings.database_url)
        if url.drivername == "sqlite+pysqlite" and (
            url.database is None or not Path(url.database).is_file()
        ):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "A migrated persistent database is required", 503
            )
        engine = build_engine(settings.database_url)
        check_database(engine)
        components = build_member_components(
            session_factory(engine), settings, slack, config.members, live_preview_origin=preview
        )
        if args.worker:
            while True:
                try:
                    result = components.worker.run_once()
                    if result is not None:
                        print(
                            json.dumps({"phase": "member_command", "state": result.state}),
                            flush=True,
                        )
                except DomainError as error:
                    print(
                        json.dumps({"phase": "member_command", "error": {"code": error.code}}),
                        file=sys.stderr,
                        flush=True,
                    )
                if args.once:
                    return 0
                time.sleep(config.members.poll_seconds)
        app: ASGIApp = create_app(
            settings,
            slack,
            user_commands=components.portal.commands,
            usage_history=components.portal.usage,
            member_portal=components.portal,
            member_paths=PUBLIC_PATHS if public else INTERNAL_PATHS,
        )
        if public:
            app = PublicPortalBoundary(app, config.members.origin, args.port)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1,::1",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        return 0
    except (ValidationError, ValueError, OSError):
        error_info = {
            "code": "CONFIGURATION_ERROR",
            "message": "Private member portal configuration is invalid",
        }
    except DomainError as error:
        error_info = {"code": error.code, "message": error.message}
    except SQLAlchemyError:
        error_info = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "Current database schema is required",
        }
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"error": error_info}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
