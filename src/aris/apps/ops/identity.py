import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
from sqlalchemy import Engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.core.access.admin_schemas import BootstrapRequest, IdentityNamespace
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory


class OperatorSettings(AccessContract):
    database_url: SecretStr = Field(repr=False)
    namespace: IdentityNamespace
    operator_key: SecretStr = Field(min_length=32, max_length=256, repr=False)

    @model_validator(mode="after")
    def fixed_slack_workspace(self) -> "OperatorSettings":
        if self.namespace.provider != "slack" or not re.fullmatch(
            r"T[A-Z0-9]{8,127}", self.namespace.namespace
        ):
            raise ValueError("operator settings require an explicit Slack workspace")
        url = make_url(self.database_url.get_secret_value())
        if url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"} or not url.database:
            raise ValueError("operator database target is invalid")
        return self


class OperatorRequest(BootstrapRequest):
    operation_id: UUID

    @model_validator(mode="after")
    def explicit_member(self) -> "OperatorRequest":
        if not re.fullmatch(r"[UW][A-Z0-9]{8,127}", self.subject.external_id):
            raise ValueError("an explicit Slack Member ID is required")
        return self


def read_private[Model: BaseModel](path: Path, model: type[Model]) -> Model:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > 65536
        ):
            raise ValueError("operator input must be an owned private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(65537)
        if len(body) > 65536:
            raise ValueError("operator input exceeds the size budget")
        return model.model_validate_json(body)
    finally:
        os.close(descriptor)


def preview(settings: OperatorSettings, request: OperatorRequest) -> dict[str, object]:
    if not hmac.compare_digest(
        settings.operator_key.get_secret_value().encode(),
        request.operator_proof.get_secret_value().encode(),
    ):
        raise DomainError("PERMISSION_DENIED", "Operator authorization is invalid", 403)
    if (request.subject.provider, request.subject.namespace) != (
        settings.namespace.provider,
        settings.namespace.namespace,
    ):
        raise DomainError("PERMISSION_DENIED", "Operator request namespace differs", 403)
    public = request.model_dump(mode="json", exclude={"operator_proof"})
    binding = {
        "database_url": settings.database_url.get_secret_value(),
        "namespace": settings.namespace.model_dump(),
        "request": public,
    }
    fingerprint = hmac.new(
        settings.operator_key.get_secret_value().encode(),
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    url = make_url(settings.database_url.get_secret_value())
    return {
        "action": "bootstrap_identity_administrator",
        "database_accessed": False,
        "namespace": settings.namespace.model_dump(),
        "request": public,
        "database_target": {
            "driver": url.drivername,
            "host": url.host or url.query.get("host"),
            "port": url.port or url.query.get("port"),
            "database": url.database,
        },
        "grants": ["identity.manage at global scope"],
        "approval_fingerprint": fingerprint,
    }


def operator_engine(settings: OperatorSettings, *, readonly: bool) -> Engine:
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername == "sqlite+pysqlite" and (
        url.database is None or not Path(url.database).is_file()
    ):
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "A migrated persistent database is required", 503
        )
    engine = build_engine(settings.database_url.get_secret_value())
    if readonly and url.drivername == "postgresql+psycopg":
        return engine.execution_options(postgresql_readonly=True)
    if readonly:

        @event.listens_for(engine, "connect")
        def read_only(connection: object, _: object) -> None:
            import sqlite3

            assert isinstance(connection, sqlite3.Connection)
            connection.execute("PRAGMA query_only=ON")

    return engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview or explicitly initialize the identity administrator"
    )
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect", action="store_true", help="Read-only inspection of the migrated directory"
    )
    mode.add_argument("--apply", action="store_true", help="Apply the reviewed operator bootstrap")
    parser.add_argument("--approve-fingerprint", help="Exact approval fingerprint from the preview")
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = read_private(args.settings, OperatorSettings)
        request = read_private(args.request, OperatorRequest)
        summary = preview(settings, request)
        if not args.inspect and not args.apply:
            print(json.dumps({"mode": "preview", **summary}))
            return 0
        if args.apply and (
            not args.approve_fingerprint
            or not re.fullmatch(r"[a-f0-9]{64}", args.approve_fingerprint)
            or not hmac.compare_digest(
                args.approve_fingerprint, str(summary["approval_fingerprint"])
            )
        ):
            raise DomainError(
                "APPROVAL_REQUIRED", "The original preview fingerprint is required", 403
            )
        engine = operator_engine(settings, readonly=args.inspect)
        check_database(engine)
        service = AccessAdministrationService(
            session_factory(engine), settings.namespace, operator_key=settings.operator_key
        )
        if args.inspect:
            outcome = service.bootstrap_status(request.operator_proof)
            print(
                json.dumps(
                    {
                        "mode": "inspect",
                        "read_only": True,
                        "configured_namespace": settings.namespace.model_dump(),
                        "namespace_matches_request": (
                            outcome.namespace == settings.namespace
                            if outcome.namespace is not None
                            else None
                        ),
                        "directory": outcome.model_dump(mode="json"),
                    }
                )
            )
        else:
            result = service.bootstrap(request)
            print(json.dumps({"mode": "apply", "result": result.model_dump(mode="json")}))
        return 0
    except (ValidationError, OSError, ValueError):
        error = {
            "code": "CONFIGURATION_ERROR",
            "message": "Private operator files are invalid or unreadable",
        }
    except DomainError as rejected:
        error = {"code": rejected.code, "message": rejected.message}
    except (SQLAlchemyError, RuntimeError):
        error = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "Database access or current schema is required",
        }
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
