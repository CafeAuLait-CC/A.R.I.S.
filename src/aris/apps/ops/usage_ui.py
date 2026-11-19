"""Manual loopback-only development entry for recorded usage. Never grants roles."""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

import uvicorn
from pydantic import Field, SecretStr, ValidationError, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.api.main import create_app
from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.activity_service import ActivityPreviewAccess, ActivityPreviewConfig
from aris.interfaces.usage.service import UsageHistorySurface
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_service import WeeklyReportReader
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.weekly_plan_reader import WeeklyPlanReader
from aris.modules.vega.usage_archive_service import VegaUsageArchive


class LocalUsageConfig(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    read_token: SecretStr = Field(min_length=32, repr=False)
    user_tokens: tuple[UserTokenBinding, ...] = Field(min_length=1, max_length=128, repr=False)
    node_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    saved_snapshots: bool = False
    weekly_reports: bool = False
    activity_preview: ActivityPreviewConfig | None = None

    @model_validator(mode="after")
    def configured_activity_nodes(self) -> "LocalUsageConfig":
        if self.activity_preview is not None and any(
            item.node_id not in self.node_ids for item in self.activity_preview.sources
        ):
            raise ValueError("activity archive source must be a configured usage node")
        return self

    def settings(self) -> Settings:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"} or not url.database:
            raise ValueError("usage interface requires an explicit persistent database")
        return Settings(
            _env_file=None,
            environment=self.environment,
            database_url=self.database_url.get_secret_value(),
            read_token=self.read_token,
            agent_credentials={},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the read-only local recorded usage interface")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=53386)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("use an unprivileged port")
    engine = None
    try:
        config = read_private(args.config, LocalUsageConfig)
        settings = config.settings()
        auth = BoundSubjectTokens(config.user_tokens, reserved_tokens=[config.read_token])
        if len(set(config.node_ids)) != len(config.node_ids):
            raise ValueError("duplicate usage nodes")
        from aris.core.access.schemas import AccessScope

        for node_id in config.node_ids:
            AccessScope(type="node", id=node_id)
        if args.validate:
            print(
                json.dumps(
                    {
                        "result": "valid",
                        "database_accessed": False,
                        "host": "127.0.0.1",
                        "port": args.port,
                    }
                )
            )
            return 0
        url = make_url(settings.database_url)
        if url.drivername == "sqlite+pysqlite" and (
            url.database is None or not Path(url.database).is_file()
        ):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Existing migrated database is required", 503
            )
        engine = build_engine(settings.database_url)
        check_database(engine)
        sessions = session_factory(engine)
        surface = UsageHistorySurface(
            UsageHistoryService(sessions),
            auth,
            config.node_ids,
            saved_windows=UsageWindowReader(session_factory(engine))
            if config.saved_snapshots
            else None,
            weekly_reports=WeeklyReportReader(UsageHistoryService(session_factory(engine)))
            if config.weekly_reports
            else None,
            weekly_plans=WeeklyPlanReader(session_factory(engine))
            if config.weekly_reports
            else None,
            activity=ActivityPreviewAccess(
                ActivityPreviewService(sessions, VegaUsageArchive(sessions)),
                config.activity_preview,
            )
            if config.activity_preview is not None
            else None,
        )
        uvicorn.run(
            create_app(
                settings,
                SlackSettings(_env_file=None, signing_secret="", bot_token="", team_id=""),
                usage_history=surface,
            ),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        return 0
    except (ValidationError, ValueError, OSError):
        error = {"code": "CONFIGURATION_ERROR", "message": "Private usage configuration is invalid"}
    except DomainError as rejected:
        error = {"code": rejected.code, "message": rejected.message}
    except SQLAlchemyError:
        error = {"code": "DEPENDENCY_UNAVAILABLE", "message": "Current database schema is required"}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
