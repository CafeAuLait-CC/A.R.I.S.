import json
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text

from aris.apps.ops import identity
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import (
    AccessDirectoryState,
    Permission,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import ExternalSubject
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from tests.test_administration import KEY, PROFILE, initialized
from tests.test_administration import admin_state as admin_state

NAMESPACE = IdentityNamespace(provider="slack", namespace="TBOOT12345")
SUBJECT = ExternalSubject(provider="slack", namespace=NAMESPACE.namespace, external_id="UBOOT1234")


def private(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def inputs(tmp_path, url):
    settings = private(
        tmp_path / "operator.json",
        {
            "database_url": url,
            "namespace": NAMESPACE.model_dump(),
            "operator_key": KEY.get_secret_value(),
        },
    )
    request = private(
        tmp_path / "request.json",
        {
            "subject": SUBJECT.model_dump(),
            "profile": PROFILE.model_dump(),
            "operator_proof": KEY.get_secret_value(),
            "request_id": "fixture-bootstrap-cli",
            "operation_id": str(uuid4()),
        },
    )
    return settings, request


def arguments(paths, *extra):
    return ["--settings", str(paths[0]), "--request", str(paths[1]), *extra]


def test_default_preview_does_not_connect_and_never_prints_credentials(
    tmp_path, monkeypatch, capsys
):
    paths = inputs(
        tmp_path, "postgresql+psycopg://user:private-database-password@database.invalid/aris"
    )

    def never(*args, **kwargs):
        raise AssertionError("pure preview must not create an engine")

    monkeypatch.setattr(identity, "operator_engine", never)
    assert identity.main(arguments(paths)) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["mode"] == "preview" and data["database_accessed"] is False
    assert data["request"]["subject"]["external_id"] == SUBJECT.external_id
    assert data["grants"] == ["identity.manage at global scope"]
    assert len(data["approval_fingerprint"]) == 64
    assert "private-database-password" not in out.out and KEY.get_secret_value() not in out.out
    assert "operator_proof" not in out.out


@pytest.mark.parametrize("change", ["subject", "profile", "database", "operation"])
def test_apply_requires_the_exact_reviewed_target_and_intent(tmp_path, monkeypatch, capsys, change):
    paths = inputs(tmp_path, "sqlite+pysqlite:///not-created.db")
    identity.main(arguments(paths))
    fingerprint = json.loads(capsys.readouterr().out)["approval_fingerprint"]
    target = paths[0] if change == "database" else paths[1]
    data = json.loads(target.read_text())
    if change == "database":
        data["database_url"] = "sqlite+pysqlite:///another-not-created.db"
    elif change == "subject":
        data["subject"]["external_id"] = "WOTHER1234"
    elif change == "profile":
        data["profile"]["display_name"] = "Changed reviewed person"
    else:
        data["operation_id"] = str(uuid4())
    private(target, data)
    monkeypatch.setattr(
        identity,
        "operator_engine",
        lambda *args, **kwargs: pytest.fail("changed review reached database"),
    )
    assert identity.main(arguments(paths, "--apply", "--approve-fingerprint", fingerprint)) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "APPROVAL_REQUIRED"


def test_inspect_is_read_only_and_explicit_apply_bootstraps_only_identity_management(
    admin_state, tmp_path, capsys
):
    _, sessions, engine = admin_state
    paths = inputs(tmp_path, engine.url.render_as_string(hide_password=False))
    assert identity.main(arguments(paths)) == 0
    fingerprint = json.loads(capsys.readouterr().out)["approval_fingerprint"]
    assert identity.main(arguments(paths, "--inspect")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["read_only"] and status["directory"]["state"] == "empty"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccessDirectoryState)) == 0
        assert session.scalar(select(func.count()).select_from(User)) == 0
    assert identity.main(arguments(paths, "--apply", "--approve-fingerprint", fingerprint)) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["state"] == "created" and result["revision"] == 1
    with sessions() as session:
        grants = session.scalars(
            select(Permission.name)
            .join(RolePermission)
            .join(RoleBinding, RoleBinding.role_id == RolePermission.role_id)
            .where(RoleBinding.user_id == result["user_id"])
        ).all()
        assert grants == ["identity.manage"]
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 1
    with sessions.begin() as session:
        session.get(User, result["user_id"]).status = "suspended"
        session.execute(delete(RoleBinding))
    assert identity.main(arguments(paths, "--apply", "--approve-fingerprint", fingerprint)) == 0
    assert json.loads(capsys.readouterr().out)["result"]["state"] == "already_initialized"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 1
        assert session.get(User, result["user_id"]).status == "suspended"
        assert session.scalar(select(func.count()).select_from(RoleBinding)) == 0


def test_existing_directory_is_not_adopted_or_reassigned_by_the_tool(admin_state, tmp_path, capsys):
    _, _, engine = admin_state
    initialized(admin_state)
    paths = inputs(tmp_path, engine.url.render_as_string(hide_password=False))
    identity.main(arguments(paths))
    fingerprint = json.loads(capsys.readouterr().out)["approval_fingerprint"]
    assert identity.main(arguments(paths, "--inspect")) == 0
    assert json.loads(capsys.readouterr().out)["directory"]["state"] == "initialized"
    assert identity.main(arguments(paths, "--apply", "--approve-fingerprint", fingerprint)) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "issue", ["permissions", "symlink", "too-large", "placeholder", "missing-operation", "proof"]
)
def test_unsafe_private_inputs_and_placeholders_fail_before_database_access(
    tmp_path, monkeypatch, capsys, issue
):
    paths = list(inputs(tmp_path, "sqlite+pysqlite:///not-created.db"))
    if issue == "permissions":
        paths[1].chmod(0o644)
    elif issue == "symlink":
        link = tmp_path / "linked.json"
        link.symlink_to(paths[1])
        paths[1] = link
    elif issue == "too-large":
        paths[1].write_text("x" * 65537)
    else:
        data = json.loads(paths[1].read_text())
        if issue == "placeholder":
            data["subject"]["external_id"] = "REPLACE_MEMBER_ID"
        elif issue == "missing-operation":
            data.pop("operation_id")
        else:
            data["operator_proof"] = "incorrect-private-proof-123456789012345"
        private(paths[1], data)
    monkeypatch.setattr(
        identity,
        "operator_engine",
        lambda *args, **kwargs: pytest.fail("unsafe input reached the database"),
    )
    assert identity.main(arguments(paths, "--inspect")) == 2
    error = capsys.readouterr().err
    assert KEY.get_secret_value() not in error and "incorrect-private-proof" not in error
    assert json.loads(error)["error"]["code"] == (
        "PERMISSION_DENIED" if issue == "proof" else "CONFIGURATION_ERROR"
    )


def test_missing_sqlite_target_is_not_created_by_inspection(tmp_path, capsys):
    target = tmp_path / "must-not-be-created.db"
    paths = inputs(tmp_path, "sqlite+pysqlite:///" + str(target))
    assert identity.main(arguments(paths, "--inspect")) == 2
    assert (
        not target.exists()
        and json.loads(capsys.readouterr().err)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    )


def test_operator_status_requires_proof_and_does_not_implicitly_initialize(admin_state):
    service, sessions, _ = admin_state
    with pytest.raises(DomainError) as refused:
        service.bootstrap_status(SecretStr("wrong-proof"))
    assert refused.value.code == "PERMISSION_DENIED"
    assert service.bootstrap_status(KEY).state == "empty"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccessDirectoryState)) == 0


def test_uninitialized_existing_users_require_adoption_without_claiming_the_directory(admin_state):
    service, sessions, _ = admin_state
    with sessions.begin() as session:
        session.add(
            User(id=str(uuid4()), display_name="Existing person", timezone="UTC", status="active")
        )
    assert service.bootstrap_status(KEY).state == "adoption_required"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccessDirectoryState)) == 0


def test_schema_mismatch_is_reported_without_automatic_migration(admin_state, tmp_path, capsys):
    _, sessions, engine = admin_state
    paths = inputs(tmp_path, engine.url.render_as_string(hide_password=False))
    with sessions.begin() as session:
        session.execute(text("UPDATE alembic_version SET version_num='0005'"))
    assert identity.main(arguments(paths, "--inspect")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    with sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
        assert session.scalar(select(func.count()).select_from(User)) == 0
