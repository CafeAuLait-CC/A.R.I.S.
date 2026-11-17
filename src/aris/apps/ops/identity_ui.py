import argparse
import json
import sys
from pathlib import Path
from typing import Literal

import uvicorn
from pydantic import Field, JsonValue, SecretStr, ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.api.main import create_app
from aris.apps.composition.m2 import load_m2_config, validate_m2_configuration
from aris.apps.ops.identity import read_private
from aris.apps.worker.main import run_m2_worker
from aris.core.access.schemas import AccessContract
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.interfaces.slack.config import SlackSettings


class LocalIdentitySettings(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    read_token: SecretStr = Field(min_length=32, repr=False)
    agent_credentials: dict[str, JsonValue] = Field(default_factory=dict, max_length=0)
    stale_after_seconds: int = 60
    offline_after_seconds: int = 180
    max_clock_skew_seconds: int = 60

    def settings(self) -> Settings:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"} or not url.database:
            raise ValueError("local identity database is invalid")
        return Settings(
            _env_file=None,
            environment=self.environment,
            database_url=self.database_url.get_secret_value(),
            read_token=self.read_token,
            agent_credentials={},
            stale_after_seconds=self.stale_after_seconds,
            offline_after_seconds=self.offline_after_seconds,
            max_clock_skew_seconds=self.max_clock_skew_seconds,
        )


class LocalSlackSettings(AccessContract):
    team_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    bot_token: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    signing_secret: SecretStr = Field(min_length=1, max_length=4096, repr=False)

    def settings(self) -> SlackSettings:
        return SlackSettings(_env_file=None, **self.model_dump())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only development identity interface"
    )
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--slack-settings", type=Path, required=True)
    parser.add_argument("--m2-config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=53385)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--validate", action="store_true", help="Validate private files without connecting"
    )
    mode.add_argument("--worker", action="store_true", help="Run only identity command jobs")
    parser.add_argument("--once", action="store_true", help="Run one worker cycle")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or (args.once and not args.worker):
        parser.error("use an unprivileged port; --once requires --worker")
    try:
        settings = read_private(args.settings, LocalIdentitySettings).settings()
        slack = read_private(args.slack_settings, LocalSlackSettings).settings()
        config = load_m2_config(args.m2_config)
        if (
            not config.identity_admin
            or config.commands != ("identity.map_users",)
            or config.slack_callbacks
            or config.notification_delivery
            or config.reservation_home
        ):
            raise ValueError("local identity runtime only permits the identity mapping interface")
        validate_m2_configuration(settings, slack, config)
        if args.validate:
            print(
                json.dumps(
                    {
                        "mode": "validation",
                        "result": "valid",
                        "database_accessed": False,
                        "host": "127.0.0.1",
                        "port": args.port,
                        "commands": list(config.commands),
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
        if args.worker:
            run_m2_worker(settings, slack, config, "commands", args.once)
        else:
            uvicorn.run(
                create_app(settings, slack, m2_config=config),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        return 0
    except (ValidationError, OSError, ValueError):
        error = {
            "code": "CONFIGURATION_ERROR",
            "message": "Private identity runtime configuration is invalid",
        }
    except DomainError as rejected:
        error = {"code": rejected.code, "message": rejected.message}
    except (SQLAlchemyError, RuntimeError):
        error = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "Database access or current schema is required",
        }
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
