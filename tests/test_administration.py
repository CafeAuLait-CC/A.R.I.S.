import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import delete, event, func, select

from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    AssignNodeUser,
    BindRole,
    BootstrapRequest,
    DefineRole,
    DirectoryProfile,
    IdentityNamespace,
    RegisterIdentity,
    RegistrationMapping,
    RemoveNodeUser,
    UpdateUser,
)
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.models import (
    AccessDirectoryState,
    ExternalIdentity,
    NodeUserMapping,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject, NodeUserLookup
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import (
    CommandContext,
    CommandDefinition,
    CommandRequest,
    ConfirmCommand,
)
from aris.core.commands.service import CommandService, DatabaseCommand
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.models import Node
from aris.modules.vega.scope_service import VegaNodeScopes

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
KEY = SecretStr("fixture-independent-operator-key-123456789")
NAMESPACE = IdentityNamespace(provider="slack", namespace="TFIXTURE")
PROFILE = DirectoryProfile(display_name="Fixture person", timezone="UTC")
GLOBAL = AccessScope(type="global", id="*")


def subject(member="UADMIN", namespace="TFIXTURE"):
    return ExternalSubject(provider="slack", namespace=namespace, external_id=member)


def bootstrap_request(member="UADMIN", **updates):
    arguments = dict(
        subject=subject(member), profile=PROFILE, operator_proof=KEY, request_id="fixture-bootstrap"
    )
    arguments.update(updates)
    return BootstrapRequest(**arguments)


def context(actor, **updates):
    return AdministrationContext(
        actor_id=actor,
        request_id="fixture-admin-change",
        reason="explicit fixture configuration",
        **updates,
    )


def batch(*changes, revision=None):
    return AdministrationBatch(changes=changes, expected_revision=revision)


@pytest.fixture
def admin_state(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    with sessions.begin() as session:
        for node in ("node-a", "node-b"):
            session.add(
                Node(
                    id=node,
                    agent_id="agent-" + node,
                    hostname="fixture",
                    agent_version="fixture",
                    last_seq=1,
                    observed_at=NOW - timedelta(days=1),
                    received_at=NOW - timedelta(days=1),
                )
            )
    catalog = VegaNodeScopes(sessions)
    service = AccessAdministrationService(
        sessions,
        NAMESPACE,
        operator_key=KEY,
        permission_catalog=("reservation.create", "directory.test"),
        scopes=catalog,
        clock=lambda: NOW,
    )
    try:
        yield service, sessions, engine
    finally:
        engine.dispose()


def initialized(state):
    service, _, _ = state
    return service.bootstrap(bootstrap_request()).user_id


def register(service, actor, member="UMEMBER", **updates):
    return service.apply(
        context(actor),
        batch(
            RegisterIdentity(
                subject=subject(member),
                profile=PROFILE,
                **updates,
            )
        ),
    )


def member_id(result):
    return result.outcomes[0].entity_id


def admin_count(sessions):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(User).where(User.status == "active"))


def assert_denied(call, code="PERMISSION_DENIED"):
    with pytest.raises(DomainError) as error:
        call()
    assert error.value.code == code


def test_bootstrap_requires_explicit_independent_operator_authorization(admin_state):
    _, sessions, _ = admin_state
    disabled = AccessAdministrationService(sessions, NAMESPACE)
    assert_denied(lambda: disabled.bootstrap(bootstrap_request()), "DEPENDENCY_UNAVAILABLE")
    service = admin_state[0]
    assert_denied(
        lambda: service.bootstrap(bootstrap_request(operator_proof=SecretStr("wrong-key-" * 5)))
    )
    assert_denied(lambda: service.bootstrap(bootstrap_request(subject=subject(namespace="TOTHER"))))
    with sessions() as session:
        assert session.get(AccessDirectoryState, "primary") is None
        assert session.scalar(select(func.count()).select_from(User)) == 0


def test_initialization_is_atomic_explicit_and_has_no_role_name_bypass(admin_state):
    service, sessions, _ = admin_state
    result = service.bootstrap(bootstrap_request())
    assert result.state == "created" and result.revision == 1
    access = AccessService(sessions)
    assert access.resolve_external(subject()).user_id == result.user_id
    assert (
        access.require_permission(
            AccessRequest(
                user_id=result.user_id,
                permission="identity.manage",
                scope=GLOBAL,
            )
        ).user_id
        == result.user_id
    )
    assert_denied(
        lambda: access.require_permission(
            AccessRequest(
                user_id=result.user_id,
                permission="reservation.create",
                scope=GLOBAL,
            )
        )
    )
    with sessions() as session:
        assert session.get(AccessDirectoryState, "primary").initial_user_id == str(result.user_id)
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 1
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 1
        assert "fixture-independent-operator-key" not in json.dumps(
            [row.changes for row in session.scalars(select(AuditEntry))]
        )
    assert "fixture-independent-operator-key" not in repr(bootstrap_request())


def test_bootstrap_retry_cannot_regrant_revoked_privileges_or_adopt_a_new_owner(admin_state):
    service, sessions, _ = admin_state
    first = service.bootstrap(bootstrap_request())
    with sessions.begin() as session:
        session.execute(delete(RolePermission))
    replay = service.bootstrap(bootstrap_request())
    assert replay.state == "already_initialized" and replay.user_id == first.user_id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(RolePermission)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 1
    assert_denied(lambda: service.bootstrap(bootstrap_request(member="UOTHER")), "CONFLICT")


def test_unmanaged_existing_identity_data_is_not_automatically_adopted(admin_state):
    service, sessions, _ = admin_state
    with sessions.begin() as session:
        session.add(User(id=str(uuid4()), display_name="Existing", status="active", timezone="UTC"))
    assert_denied(lambda: service.bootstrap(bootstrap_request()), "CONFLICT")
    with sessions() as session:
        assert session.get(AccessDirectoryState, "primary") is None
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_registration_and_two_node_mappings_are_one_atomic_change_without_grants(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    result = register(
        service,
        actor,
        mappings=(
            RegistrationMapping(node_id="node-a", os_username="alice_a"),
            RegistrationMapping(node_id="node-b", os_username="different_b"),
        ),
    )
    user = member_id(result)
    access = AccessService(sessions)
    assert result.changed and result.revision == 2
    for node, name in (("node-a", "alice_a"), ("node-b", "different_b")):
        assert (
            str(access.resolve_node_user(NodeUserLookup(node_id=node, os_username=name)).user_id)
            == user
        )
    assert access.resolve_node_user(NodeUserLookup(node_id="node-b", os_username="alice_a")) is None
    assert_denied(
        lambda: access.require_permission(
            AccessRequest(
                user_id=user,
                permission="identity.manage",
                scope=GLOBAL,
            )
        )
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 2
        assert "alice_a" not in json.dumps([x.changes for x in session.scalars(select(AuditEntry))])


@pytest.mark.parametrize("problem", ["scope", "duplicate_owner", "missing_catalog"])
def test_registration_rolls_back_if_any_mapping_cannot_be_verified(admin_state, problem):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    if problem == "duplicate_owner":
        service.apply(
            context(actor),
            batch(
                AssignNodeUser(
                    user_id=actor,
                    node_id="node-b",
                    os_username="claimed",
                )
            ),
        )
    if problem == "missing_catalog":
        service.scopes = None
    before = admin_count(sessions)
    mappings = (
        RegistrationMapping(node_id="node-a", os_username="first"),
        RegistrationMapping(
            node_id="unknown" if problem == "scope" else "node-b",
            os_username="claimed" if problem == "duplicate_owner" else "second",
        ),
    )
    assert_denied(
        lambda: register(service, actor, mappings=mappings),
        {
            "scope": "VALIDATION_ERROR",
            "duplicate_owner": "CONFLICT",
            "missing_catalog": "DEPENDENCY_UNAVAILABLE",
        }[problem],
    )
    assert admin_count(sessions) == before
    assert AccessService(sessions).resolve_external(subject("UMEMBER")) is None
    with sessions() as session:
        assert (
            session.scalar(select(NodeUserMapping).where(NodeUserMapping.os_username == "first"))
            is None
        )


def test_same_username_is_scoped_and_reassignment_requires_the_expected_owner(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    service.apply(
        context(actor),
        batch(
            AssignNodeUser(user_id=actor, node_id="node-a", os_username="same"),
            AssignNodeUser(user_id=user, node_id="node-b", os_username="same"),
        ),
    )
    assert_denied(
        lambda: service.apply(
            context(actor),
            batch(
                AssignNodeUser(
                    user_id=user,
                    node_id="node-a",
                    os_username="same",
                )
            ),
        ),
        "CONFLICT",
    )
    result = service.apply(
        context(actor),
        batch(
            AssignNodeUser(
                user_id=user,
                node_id="node-a",
                os_username="same",
                expected_user_id=actor,
            )
        ),
    )
    assert result.changed
    assert (
        str(
            AccessService(sessions)
            .resolve_node_user(
                NodeUserLookup(
                    node_id="node-a",
                    os_username="same",
                )
            )
            .user_id
        )
        == user
    )
    assert_denied(
        lambda: service.apply(
            context(actor),
            batch(
                RemoveNodeUser(
                    node_id="node-a",
                    os_username="same",
                    expected_user_id=actor,
                )
            ),
        ),
        "CONFLICT",
    )
    service.apply(
        context(actor),
        batch(
            RemoveNodeUser(
                node_id="node-a",
                os_username="same",
                expected_user_id=user,
            )
        ),
    )
    assert (
        AccessService(sessions).resolve_node_user(
            NodeUserLookup(node_id="node-a", os_username="same")
        )
        is None
    )


def test_registration_does_not_reactivate_or_rename_an_existing_identity(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    service.apply(
        context(actor),
        batch(
            UpdateUser(
                user_id=user,
                profile=DirectoryProfile(display_name="Manual name", timezone="Europe/London"),
                status="suspended",
            )
        ),
    )
    again = register(service, actor)
    assert not again.changed and member_id(again) == user
    assert AccessService(sessions).resolve_external(subject("UMEMBER")) is None
    with sessions() as session:
        stored = session.get(User, user)
        assert stored.status == "suspended" and stored.display_name == "Manual name"


def test_reassignment_audit_records_both_owners_without_copying_linux_names(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    service.apply(
        context(actor),
        batch(AssignNodeUser(user_id=actor, node_id="node-a", os_username="private_linux_name")),
    )
    audit_context = context(actor)
    result = service.apply(
        audit_context,
        batch(
            AssignNodeUser(
                user_id=user,
                node_id="node-a",
                os_username="private_linux_name",
                expected_user_id=actor,
            ),
            DefineRole(name="member", permissions=("reservation.create",)),
            DefineRole(name="member", permissions=("directory.test",)),
        ),
    )
    # Mutating a returned JSON container cannot rewrite the persisted audit snapshot.
    result.outcomes[0].details["previous_user_id"] = "changed-after-return"
    with sessions() as session:
        entry = session.scalar(
            select(AuditEntry).where(
                AuditEntry.changes["operation_id"].as_string() == str(audit_context.operation_id)
            )
        )
        previous, _, role = entry.changes["outcomes"]
        assert previous["details"]["previous_user_id"] == str(actor)
        assert previous["details"]["user_id"] == user
        assert len(previous["details"]["mapping_key"]) == 64
        assert role["details"]["previous_permissions"] == ["reservation.create"]
        assert role["details"]["permissions"] == ["directory.test"]
        assert "private_linux_name" not in json.dumps(entry.changes)


def test_noop_is_audited_without_revising_or_emitting_another_change(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    first = register(service, actor)
    again = register(service, actor)
    assert not again.changed and again.revision == first.revision
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 2
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 3
        noop = session.scalar(
            select(AuditEntry).where(AuditEntry.action == "access.directory.noop")
        )
        assert noop.changes["outcomes"][0]["changed"] is False


def test_global_management_grant_does_not_adopt_a_foreign_workspace_actor(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    service.apply(
        context(actor),
        batch(BindRole(user_id=user, role_name="identity_admin", scope=GLOBAL, present=True)),
    )
    with sessions.begin() as session:
        identity = session.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == user))
        identity.namespace = "TOTHER"
    assert_denied(
        lambda: service.apply(
            context(user), batch(DefineRole(name="unexpected", permissions=("directory.test",)))
        )
    )
    with sessions() as session:
        assert session.scalar(select(Role).where(Role.name == "unexpected")) is None
        assert session.get(AccessDirectoryState, "primary").revision == 3


def test_public_node_scope_catalog_does_not_flush_or_commit_owner_work(admin_state):
    _, sessions, engine = admin_state
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with sessions() as session:
            pending = User(id=str(uuid4()), display_name="Pending", status="active", timezone="UTC")
            session.add(pending)
            assert VegaNodeScopes(sessions).known_in_transaction(
                session, AccessScope(type="node", id="node-a")
            )
            assert pending in session.new
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
            session.rollback()
        with sessions() as session:
            assert session.get(User, pending.id) is None
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_stale_directory_revision_and_unknown_permissions_are_refused(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    register(service, actor)
    assert_denied(
        lambda: service.apply(
            context(actor),
            batch(
                DefineRole(name="member", permissions=("reservation.create",)),
                revision=1,
            ),
        ),
        "CONFLICT",
    )
    assert_denied(
        lambda: service.apply(
            context(actor),
            batch(
                DefineRole(name="invented", permissions=("unregistered.shell",)),
            ),
        ),
        "VALIDATION_ERROR",
    )
    with sessions() as session:
        assert session.scalar(select(Role).where(Role.name.in_(("member", "invented")))) is None


def test_roles_require_exact_current_scope_and_revocation_applies_immediately(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    service.apply(
        context(actor),
        batch(
            DefineRole(name="member", permissions=("reservation.create",)),
            BindRole(
                user_id=user,
                role_name="member",
                scope=AccessScope(type="node", id="node-a"),
                present=True,
            ),
        ),
    )
    access = AccessService(sessions)
    assert access.require_permission(
        AccessRequest(
            user_id=user,
            permission="reservation.create",
            scope=AccessScope(type="node", id="node-a"),
        )
    )
    assert_denied(
        lambda: access.require_permission(
            AccessRequest(
                user_id=user,
                permission="reservation.create",
                scope=AccessScope(type="node", id="node-b"),
            )
        )
    )
    service.apply(
        context(actor),
        batch(
            BindRole(
                user_id=user,
                role_name="member",
                scope=AccessScope(type="node", id="node-a"),
                present=False,
            )
        ),
    )
    assert_denied(
        lambda: access.require_permission(
            AccessRequest(
                user_id=user,
                permission="reservation.create",
                scope=AccessScope(type="node", id="node-a"),
            )
        )
    )


@pytest.mark.parametrize("change", ["suspend", "binding", "role"])
def test_last_active_login_capable_global_admin_cannot_be_removed(admin_state, change):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    operation = (
        UpdateUser(user_id=actor, profile=PROFILE, status="suspended")
        if change == "suspend"
        else BindRole(user_id=actor, role_name="identity_admin", scope=GLOBAL, present=False)
        if change == "binding"
        else DefineRole(name="identity_admin", permissions=())
    )
    assert_denied(lambda: service.apply(context(actor), batch(operation)), "CONFLICT")
    assert AccessService(sessions).require_permission(
        AccessRequest(
            user_id=actor,
            permission="identity.manage",
            scope=GLOBAL,
        )
    )
    with sessions() as session:
        assert session.get(AccessDirectoryState, "primary").revision == 1
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 1


def test_unprivileged_actor_or_node_only_management_grant_cannot_write(admin_state):
    service, _, _ = admin_state
    actor = initialized(admin_state)
    user = member_id(register(service, actor))
    change = batch(DefineRole(name="member", permissions=("reservation.create",)))
    assert_denied(lambda: service.apply(context(user), change))
    service.apply(
        context(actor),
        batch(
            BindRole(
                user_id=user,
                role_name="identity_admin",
                scope=AccessScope(type="node", id="node-a"),
                present=True,
            )
        ),
    )
    assert_denied(lambda: service.apply(context(user), change))


def test_caught_batch_failure_cannot_commit_partial_changes(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    with sessions.begin() as session:
        with pytest.raises(DomainError):
            service.apply_in_transaction(
                session,
                context(actor),
                batch(
                    RegisterIdentity(subject=subject("UTEMP"), profile=PROFILE),
                    DefineRole(name="identity_admin", permissions=()),
                ),
            )
        # Owner can still commit unrelated work; the failed savepoint stays rolled back.
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor="fixture",
                action="fixture.owner",
                target="fixture",
                request_id="fixture-owner",
                recorded_at=NOW,
                changes={},
            )
        )
    assert AccessService(sessions).resolve_external(subject("UTEMP")) is None
    assert AccessService(sessions).require_permission(
        AccessRequest(
            user_id=actor,
            permission="identity.manage",
            scope=GLOBAL,
        )
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 2


def test_savepoint_release_does_not_escape_an_owner_rollback_even_on_sqlite(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    with pytest.raises(RuntimeError), sessions.begin() as session:
        service.apply_in_transaction(
            session,
            context(actor),
            batch(
                RegisterIdentity(
                    subject=subject("UTEMP"),
                    profile=PROFILE,
                )
            ),
        )
        raise RuntimeError("fixture owner abort")
    assert AccessService(sessions).resolve_external(subject("UTEMP")) is None
    with sessions() as session:
        assert session.get(AccessDirectoryState, "primary").revision == 1


def test_user_administration_composes_with_confirmed_idempotent_commands(admin_state):
    service, sessions, _ = admin_state
    actor = initialized(admin_state)
    admin_context = context(actor)

    class Registration(DatabaseCommand):
        definition = CommandDefinition(
            name="identity.register", required_permission="identity.manage"
        )

        def normalize(self, parameters):
            value = RegisterIdentity.model_validate(parameters)
            return value.model_dump(mode="json")

        def authorization_scope(self, session, parameters):
            return GLOBAL

        def execute(self, session, execution, principal, parameters):
            assert principal.user_id == admin_context.actor_id
            result = service.apply_in_transaction(
                session,
                admin_context,
                batch(
                    RegisterIdentity.model_validate(parameters),
                ),
            )
            return result.model_dump(mode="json")

    commands = CommandService(
        sessions, [Registration()], confirmation_key=SecretStr("fixture-confirmation-key-123456789")
    )
    cmd_context = CommandContext(
        actor_id=actor, request_id="fixture-request", idempotency_key="fixture-once"
    )
    request = CommandRequest(
        command="identity.register",
        scope=GLOBAL,
        parameters=RegisterIdentity(subject=subject("UCONFIRM"), profile=PROFILE).model_dump(
            mode="json"
        ),
    )
    pending = commands.execute(cmd_context, request)
    assert AccessService(sessions).resolve_external(subject("UCONFIRM")) is None
    result = commands.confirm(
        cmd_context, ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )
    assert result.state == "succeeded"
    retry = commands.execute(cmd_context, request)
    assert retry.duplicate and retry.result == result.result
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 1
        assert session.get(AccessDirectoryState, "primary").revision == 2


def test_administration_history_refuses_destructive_downgrade(admin_state):
    command.downgrade(Config("alembic.ini"), "0010")
    actor = initialized(admin_state)
    with pytest.raises(RuntimeError, match="identity administration history"):
        command.downgrade(Config("alembic.ini"), "0009")
    with admin_state[1]() as session:
        state = session.get(AccessDirectoryState, "primary")
        assert state.initial_user_id == str(actor) and state.revision == 1
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))


def test_empty_state_migration_round_trip_and_drift(settings):
    config = Config("alembic.ini")
    command.downgrade(config, "0009")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize("race", ["bootstrap", "bootstrap_retry", "self_revoke", "mapping"])
def test_postgres_serializes_identity_writes_without_losing_ownership(admin_state, race):
    service, sessions, engine = admin_state
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL directory row locks")
    barrier = Barrier(2)
    if race.startswith("bootstrap"):

        def attempt(member):
            barrier.wait(timeout=5)
            try:
                return service.bootstrap(bootstrap_request(member)).state
            except DomainError as error:
                return error.code

        members = ("UONE", "UONE") if race == "bootstrap_retry" else ("UONE", "UTWO")
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, members))
        assert sorted(outcomes) == (
            ["already_initialized", "created"]
            if race == "bootstrap_retry"
            else ["CONFLICT", "created"]
        )
    elif race == "mapping":
        first = initialized(admin_state)
        second = member_id(register(service, first, member="UTWO"))

        def attempt(owner):
            barrier.wait(timeout=5)
            try:
                service.apply(
                    context(first),
                    batch(AssignNodeUser(user_id=owner, node_id="node-a", os_username="shared")),
                )
                return owner
            except DomainError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (str(first), second)))
        assert outcomes.count("CONFLICT") == 1
        winner = next(owner for owner in outcomes if owner != "CONFLICT")
        linked = AccessService(sessions).resolve_node_user(
            NodeUserLookup(node_id="node-a", os_username="shared")
        )
        assert str(linked.user_id) == winner
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(NodeUserMapping)) == 1
    else:
        first = initialized(admin_state)
        second = member_id(register(service, first, member="UTWO"))
        service.apply(
            context(first),
            batch(
                BindRole(
                    user_id=second,
                    role_name="identity_admin",
                    scope=GLOBAL,
                    present=True,
                )
            ),
        )

        def attempt(actor):
            barrier.wait(timeout=5)
            try:
                service.apply(
                    context(actor),
                    batch(
                        BindRole(
                            user_id=actor,
                            role_name="identity_admin",
                            scope=GLOBAL,
                            present=False,
                        )
                    ),
                )
                return "changed"
            except DomainError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (first, second)))
        assert sorted(outcomes) == ["CONFLICT", "changed"]
    with sessions() as session:
        admins = session.scalar(
            select(func.count())
            .select_from(RoleBinding)
            .where(
                RoleBinding.scope_type == "global",
                RoleBinding.scope_id == "*",
            )
        )
        assert admins == 1
