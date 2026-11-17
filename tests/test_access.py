import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from aris.core.access.models import (
    ExternalIdentity,
    NodeUserMapping,
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject, NodeUserLookup
from aris.core.access.service import AccessService
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory
from tests.test_telemetry import batch, post


@pytest.fixture
def access(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    ids = {key: str(uuid4()) for key in ("alice", "bob", "member", "admin", "create")}
    with sessions.begin() as db:
        db.add_all(
            [
                User(
                    id=ids["alice"],
                    display_name="Same display name",
                    status="active",
                    timezone="UTC",
                ),
                User(
                    id=ids["bob"],
                    display_name="Same display name",
                    status="active",
                    timezone="America/Vancouver",
                ),
                Role(id=ids["member"], name="member"),
                Role(id=ids["admin"], name="admin"),
                Permission(id=ids["create"], name="reservation.create"),
            ]
        )
        db.flush()
        db.add_all(
            [
                ExternalIdentity(
                    id=str(uuid4()),
                    provider="slack",
                    namespace="TONE",
                    external_id="USAME",
                    user_id=ids["alice"],
                ),
                ExternalIdentity(
                    id=str(uuid4()),
                    provider="slack",
                    namespace="TTWO",
                    external_id="USAME",
                    user_id=ids["bob"],
                ),
                RolePermission(role_id=ids["member"], permission_id=ids["create"]),
                RoleBinding(
                    id=str(uuid4()),
                    user_id=ids["alice"],
                    role_id=ids["member"],
                    scope_type="node",
                    scope_id="node-a",
                ),
                RoleBinding(
                    id=str(uuid4()),
                    user_id=ids["bob"],
                    role_id=ids["admin"],
                    scope_type="global",
                    scope_id="*",
                ),
                NodeUserMapping(
                    node_id="node-a",
                    os_username="shared",
                    user_id=ids["alice"],
                    confidence="verified",
                    source="manual",
                ),
                NodeUserMapping(
                    node_id="node-b",
                    os_username="shared",
                    user_id=ids["bob"],
                    confidence="unverified",
                    source="manual",
                ),
            ]
        )
    try:
        yield AccessService(sessions), sessions, ids
    finally:
        engine.dispose()


def request(ids, user="alice", permission="reservation.create", kind="node", scope="node-a"):
    return AccessRequest(
        user_id=ids[user], permission=permission, scope=AccessScope(type=kind, id=scope)
    )


def denied(service, query):
    with pytest.raises(DomainError) as error:
        service.require_permission(query)
    assert (error.value.code, error.value.status) == ("PERMISSION_DENIED", 403)
    assert error.value.message == "Permission is not granted"


def test_external_identity_is_workspace_scoped_and_never_enrolls_by_display_name(access):
    service, sessions, ids = access
    first = service.resolve_external(
        ExternalSubject(provider="slack", namespace="TONE", external_id="USAME")
    )
    second = service.resolve_external(
        ExternalSubject(provider="slack", namespace="TTWO", external_id="USAME")
    )
    assert str(first.user_id) == ids["alice"] and str(second.user_id) == ids["bob"]
    assert first.display_name == second.display_name
    assert (
        service.resolve_external(
            ExternalSubject(provider="slack", namespace="TTHREE", external_id="USAME")
        )
        is None
    )
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 2
    denied(service, request(ids, user="bob"))


@pytest.mark.parametrize(
    "changes", [{"provider": "other"}, {"namespace": "tone"}, {"external_id": "usame"}]
)
def test_external_identifiers_are_exact_and_not_inferred(access, changes):
    service, _, _ = access
    subject = {"provider": "slack", "namespace": "TONE", "external_id": "USAME", **changes}
    assert service.resolve_external(ExternalSubject(**subject)) is None


def test_node_username_mapping_is_explicit_scoped_and_not_a_permission_grant(access):
    service, _, ids = access
    principal = service.resolve_node_user(NodeUserLookup(node_id="node-a", os_username="shared"))
    assert str(principal.user_id) == ids["alice"]
    for node, name in [
        ("node-b", "shared"),
        ("other", "shared"),
        ("node-a", "Shared"),
        ("node-a", "missing"),
    ]:
        assert service.resolve_node_user(NodeUserLookup(node_id=node, os_username=name)) is None
    denied(service, request(ids, permission="reservation.cancel_own"))


@pytest.mark.parametrize(
    "confidence,source",
    [("unverified", "manual"), ("verified", "inferred"), ("verified", "directory")],
)
def test_unverified_or_unsupported_mapping_sources_are_not_identity_authority(
    access, confidence, source
):
    service, sessions, _ = access
    with sessions.begin() as db:
        mapping = db.get(NodeUserMapping, ("node-a", "shared"))
        mapping.confidence, mapping.source = confidence, source
    assert service.resolve_node_user(NodeUserLookup(node_id="node-a", os_username="shared")) is None


@pytest.mark.parametrize(
    "kind,scope", [("node", "node-b"), ("node", "NODE-A"), ("project", "node-a"), ("global", "*")]
)
def test_node_grant_never_expands_to_other_scopes(access, kind, scope):
    service, _, ids = access
    assert str(service.require_permission(request(ids)).user_id) == ids["alice"]
    denied(service, request(ids, kind=kind, scope=scope))


def test_global_grant_is_explicit_and_role_named_admin_has_no_bypass(access):
    service, sessions, ids = access
    denied(service, request(ids, user="bob"))
    with sessions.begin() as db:
        db.add(RolePermission(role_id=ids["admin"], permission_id=ids["create"]))
    for kind, scope in [("global", "*"), ("node", "node-b"), ("project", "project-b")]:
        assert (
            str(
                service.require_permission(request(ids, user="bob", kind=kind, scope=scope)).user_id
            )
            == ids["bob"]
        )
    denied(service, request(ids, user="bob", permission="reservation.cancel_own"))


def test_project_grant_is_exact_and_never_infers_node_membership(access):
    service, sessions, ids = access
    with sessions.begin() as db:
        db.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=ids["alice"],
                role_id=ids["member"],
                scope_type="project",
                scope_id="project-a",
            )
        )
    assert service.require_permission(request(ids, kind="project", scope="project-a"))
    denied(service, request(ids, kind="project", scope="project-b"))
    denied(service, request(ids, kind="node", scope="project-a"))


@pytest.mark.parametrize("revocation", ["permission", "binding", "suspension"])
def test_current_grants_are_rechecked_and_a_prior_principal_is_not_a_ticket(access, revocation):
    service, sessions, ids = access
    previous = service.require_permission(request(ids))
    with sessions.begin() as db:
        if revocation == "permission":
            db.execute(delete(RolePermission))
        elif revocation == "binding":
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        else:
            db.get(User, ids["alice"]).status = "suspended"
    denied(service, request(ids))
    assert str(previous.user_id) == ids["alice"]
    if revocation == "suspension":
        assert (
            service.resolve_external(
                ExternalSubject(provider="slack", namespace="TONE", external_id="USAME")
            )
            is None
        )
        assert (
            service.resolve_node_user(NodeUserLookup(node_id="node-a", os_username="shared"))
            is None
        )


def test_unknown_user_and_ungranted_permission_fail_with_the_same_public_error(access):
    service, _, ids = access
    denied(service, request({**ids, "alice": str(uuid4())}))
    denied(service, request(ids, permission="arbitrary.execute"))


@pytest.mark.parametrize(
    "kind,scope",
    [("global", "node-a"), ("node", "*"), ("project", "*"), ("node", ""), ("node", " ")],
)
def test_ambiguous_and_wildcard_scopes_are_rejected_before_queries(kind, scope):
    with pytest.raises(ValidationError):
        AccessScope(type=kind, id=scope)


@pytest.mark.parametrize(
    "permission",
    ["*", "reservation.*", "Reservation.create", "reservation", "reservation.create extra"],
)
def test_permissions_have_no_pattern_language(permission):
    with pytest.raises(ValidationError):
        AccessRequest(
            user_id=uuid4(), permission=permission, scope=AccessScope(type="global", id="*")
        )


def test_corrupt_stored_profile_fails_closed_without_leaking_stored_values(access):
    service, sessions, ids = access
    with sessions.begin() as db:
        db.get(User, ids["alice"]).timezone = "private-invalid-timezone"
    with pytest.raises(DomainError) as error:
        service.require_permission(request(ids))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert "private-invalid-timezone" not in str(error.value)


def test_database_rejects_duplicate_identity_binding_and_ambiguous_grants(access):
    _, sessions, ids = access
    rows = [
        ExternalIdentity(
            id=str(uuid4()),
            provider="slack",
            namespace="TONE",
            external_id="USAME",
            user_id=ids["bob"],
        ),
        RoleBinding(
            id=str(uuid4()),
            user_id=ids["alice"],
            role_id=ids["member"],
            scope_type="node",
            scope_id="node-a",
        ),
        RoleBinding(
            id=str(uuid4()),
            user_id=ids["alice"],
            role_id=ids["member"],
            scope_type="node",
            scope_id="*",
        ),
        Permission(id=str(uuid4()), name="*"),
        NodeUserMapping(
            node_id="node-a",
            os_username="shared",
            user_id=ids["bob"],
            confidence="verified",
            source="manual",
        ),
    ]
    for row in rows:
        with pytest.raises(IntegrityError), sessions.begin() as db:
            db.add(row)


def test_postgres_concurrent_identity_linking_cannot_bind_one_subject_to_two_users(access):
    service, sessions, ids = access
    with sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL unique-key conflict concurrency")
    start = Barrier(2)

    def link(user):
        start.wait(timeout=10)
        try:
            with sessions.begin() as db:
                db.add(
                    ExternalIdentity(
                        id=str(uuid4()),
                        provider="slack",
                        namespace="TONE",
                        external_id="URACE",
                        user_id=ids[user],
                    )
                )
            return user
        except IntegrityError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(link, ("alice", "bob")))
    winners = [user for user in outcomes if user is not None]
    assert len(winners) == 1
    principal = service.resolve_external(
        ExternalSubject(provider="slack", namespace="TONE", external_id="URACE")
    )
    assert str(principal.user_id) == ids[winners[0]]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 3


def test_identity_migration_is_additive_empty_and_preserves_existing_facts_at_head(client):
    original = batch()
    assert post(client, original).status_code == 200
    engine = client.app.state.engine
    legacy = [
        "agent_messages",
        "audit_logs",
        "domain_events",
        "event_outbox",
        "gpu_devices",
        "node_resources",
        "nodes",
        "slack_home_jobs",
        "slack_receipts",
    ]

    def facts():
        with engine.connect() as connection:
            return {
                table: sorted(
                    json.dumps(dict(row._mapping), sort_keys=True, default=str)
                    for row in connection.execute(text('SELECT * FROM "' + table + '"'))
                )
                for table in legacy
            }

    frozen = facts()
    with engine.connect() as connection:
        for table in (
            "users",
            "external_identities",
            "roles",
            "permissions",
            "role_permissions",
            "role_bindings",
            "node_user_mappings",
        ):
            assert connection.scalar(text('SELECT count(*) FROM "' + table + '"')) == 0
    config = Config("alembic.ini")
    command.downgrade(config, "0005")
    assert client.get("/health/ready").status_code == 503
    assert facts() == frozen
    command.upgrade(config, "head")
    command.check(config)
    assert client.get("/health/ready").json()["schema_revision"] == "0013"
    assert facts() == frozen
    assert post(client, original).json()["duplicate"] is True
