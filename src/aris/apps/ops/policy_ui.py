"""Manual loopback-only development entry for policy sources. Never grants roles."""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

import uvicorn
from pydantic import Field, SecretStr, ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.api.main import create_app
from aris.apps.composition.policy import (
    PolicyDraftRuntimeConfig,
    build_policy_draft_components,
    validate_policy_draft_configuration,
)
from aris.apps.ops.identity import read_private
from aris.apps.worker.policy import run_policy_draft_worker
from aris.core.access.schemas import AccessContract
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.policy.history import HistoricalPolicyNode, PolicyHistoricalUsage
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.lyra.quota_evidence_service import HistoricalQuotaEvidenceService
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from aris.modules.nova.policy_material_schemas import ImplementationReference
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from aris.modules.nova.quota_history_service import HistoricalQuotaAssessmentService


class LocalPolicyConfig(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    read_token: SecretStr = Field(min_length=32, repr=False)
    user_tokens: tuple[UserTokenBinding, ...] = Field(min_length=1, max_length=128, repr=False)
    policy_names: tuple[str, ...] = Field(min_length=1, max_length=32)
    draft_commands: PolicyDraftRuntimeConfig | None = None
    historical_usage: tuple[HistoricalPolicyNode, ...] = Field(default=(), max_length=8)
    review_materials_read: bool = Field(default=False, strict=True)
    material_comparison_implementation_ref: ImplementationReference | None = None

    def settings(self) -> Settings:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"} or not url.database:
            raise ValueError("policy interface requires an explicit persistent database")
        return Settings(
            _env_file=None,
            environment=self.environment,
            database_url=self.database_url.get_secret_value(),
            read_token=self.read_token,
            agent_credentials={},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the local policy interface; editing is opt-in"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=53388)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--worker", action="store_true", help="Run only confirmed draft commands")
    parser.add_argument("--once", action="store_true", help="Run a single worker cycle")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("use an unprivileged port")
    if args.once and not args.worker:
        parser.error("--once requires --worker")
    engine = None
    try:
        config = read_private(args.config, LocalPolicyConfig)
        settings = config.settings()
        auth = BoundSubjectTokens(config.user_tokens, reserved_tokens=[config.read_token])
        if len(set(config.policy_names)) != len(config.policy_names):
            raise ValueError("duplicate policy names")
        if len({node.node_id for node in config.historical_usage}) != len(config.historical_usage):
            raise ValueError("duplicate historical usage nodes")
        from aris.modules.nova.policy_catalog_schemas import PolicySourceReference

        for name in config.policy_names:
            PolicySourceReference(name=name, version=1)
        if (
            config.material_comparison_implementation_ref is not None
            and not config.review_materials_read
        ):
            raise ValueError("material comparisons require explicit material reads")
        if config.draft_commands is not None:
            validate_policy_draft_configuration(config.draft_commands, auth, config.read_token)
            if (
                config.draft_commands.review_material_implementation_ref is not None
                and not config.review_materials_read
            ):
                raise ValueError("material writing requires its explicit read interface")
        elif args.worker:
            raise ValueError("draft commands must be explicitly configured for the worker")
        if args.validate:
            summary: dict[str, object] = {
                "result": "valid",
                "database_accessed": False,
                "host": "127.0.0.1",
                "port": args.port,
            }
            if config.draft_commands is not None:
                summary["draft_commands"] = ["policy.record_draft"]
                if config.draft_commands.review_material_implementation_ref is not None:
                    summary["draft_commands"] = [
                        "policy.record_draft",
                        "policy.record_review_material",
                    ]
            if config.historical_usage:
                summary["historical_usage_nodes"] = [
                    node.node_id for node in config.historical_usage
                ]
            if config.review_materials_read:
                summary["review_materials_read"] = True
            if config.material_comparison_implementation_ref is not None:
                summary["material_comparison_read"] = True
            print(json.dumps(summary))
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
        drafts = (
            build_policy_draft_components(
                sessions, auth, config.policy_names, config.draft_commands
            )
            if config.draft_commands is not None
            else None
        )
        if args.worker:
            assert drafts is not None and config.draft_commands is not None
            run_policy_draft_worker(
                drafts.worker, once=args.once, poll_seconds=config.draft_commands.poll_seconds
            )
            return 0
        catalog = PolicyCatalogService(sessions)
        history = None
        if config.historical_usage:
            reader = HistoricalQuotaEvidenceService(sessions)
            history = PolicyHistoricalUsage(
                HistoricalQuotaAssessmentService(catalog, reader), reader, config.historical_usage
            )
        surface = PolicyReadSurface(
            catalog,
            auth,
            config.policy_names,
            draft_commands=drafts.user_api if drafts is not None else None,
            historical_usage=history,
            review_materials=PolicyReviewMaterialService(catalog)
            if config.review_materials_read
            else None,
            material_comparison_implementation_ref=config.material_comparison_implementation_ref,
        )
        uvicorn.run(
            create_app(
                settings,
                SlackSettings(_env_file=None, signing_secret="", bot_token="", team_id=""),
                policy_sources=surface,
                user_commands=drafts.user_api if drafts is not None else None,
            ),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        return 0
    except (ValidationError, ValueError, OSError):
        error = {
            "code": "CONFIGURATION_ERROR",
            "message": "Private policy configuration is invalid",
        }
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
