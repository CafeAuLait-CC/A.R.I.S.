import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import Field
from sqlalchemy import delete, func, select

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission, User
from aris.core.access.schemas import AccessContract, AccessScope
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import CommandContext, CommandDefinition, CommandRequest
from aris.core.commands.service import CommandService, DatabaseCommand
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory


class RenameParameters(AccessContract):
    name: str = Field(min_length=1, max_length=128)


class RenameFixture(DatabaseCommand):
    # Explicit test-only registration, never installed into the application API.
    definition = CommandDefinition(
        name="profile.rename", required_permission="profile.write", requires_confirmation=False
    )

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.bad_result = None

    def normalize(self, parameters):
        name = RenameParameters.model_validate(parameters).name.strip()
        if not name:
            raise ValueError("empty name")
        return {"name": name}

    def authorization_scope(self, session, parameters):
        return AccessScope(type="global", id="*")

    def execute(self, session, execution, principal, parameters):
        self.calls += 1
        user = session.get(User, str(principal.user_id))
        user.display_name = parameters["name"]
        if self.fail:
            raise RuntimeError("private-handler-detail")
        return self.bad_result if self.bad_result is not None else {"name": user.display_name}


@pytest.fixture
def command_state(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    ids = {name: str(uuid4()) for name in ("alice", "bob", "role", "permission")}
    with sessions.begin() as db:
        db.add_all(
            [
                User(id=ids["alice"], display_name="Original A", status="active", timezone="UTC"),
                User(id=ids["bob"], display_name="Original B", status="active", timezone="UTC"),
                Role(id=ids["role"], name="fixture-profile-editor"),
                Permission(id=ids["permission"], name="profile.write"),
            ]
        )
        db.flush()
        db.add(RolePermission(role_id=ids["role"], permission_id=ids["permission"]))
        for name in ("alice", "bob"):
            db.add(
                RoleBinding(
                    id=str(uuid4()),
                    user_id=ids[name],
                    role_id=ids["role"],
                    scope_type="global",
                    scope_id="*",
                )
            )
    action = RenameFixture()
    try:
        yield CommandService(sessions, [action]), sessions, ids, action
    finally:
        engine.dispose()


def context(ids, user="alice", key="fixture-private-key", request_id="request-first"):
    return CommandContext(actor_id=ids[user], request_id=request_id, idempotency_key=key)


def request(name="Renamed", **changes):
    return CommandRequest(
        command="profile.rename",
        scope=AccessScope(type="global", id="*"),
        parameters={"name": name},
        **changes,
    )


def counts(sessions):
    with sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (CommandRecord, AuditEntry)
        )


def test_repeat_uses_canonical_parameters_original_result_and_one_atomic_audit(command_state):
    service, sessions, ids, action = command_state
    original = request("  Renamed  ")
    first = service.execute(context(ids), original)
    assert original.parameters == {"name": "  Renamed  "}
    second = service.execute(context(ids, request_id="request-retry"), request())
    assert not first.duplicate and second.duplicate
    assert second.command_id == first.command_id and first.result == second.result == {
        "name": "Renamed"
    }
    assert action.calls == 1 and counts(sessions) == (1, 1)
    with sessions() as db:
        record = db.get(CommandRecord, str(first.command_id))
        assert record.state == "succeeded" and record.completed_at is not None
        assert record.parameters == {"name": "Renamed"} and record.request_id == "request-first"
        assert record.key_sha256 == hashlib.sha256(b"fixture-private-key").hexdigest()
        audit = db.scalars(select(AuditEntry)).one()
        assert audit.actor == ids["alice"] and audit.action == "command.succeeded"
        assert "fixture-private-key" not in repr(record.__dict__) + repr(audit.__dict__)
        assert "parameters" not in audit.changes


@pytest.mark.parametrize("change", ["parameters", "scope", "command"])
def test_reused_key_with_changed_meaning_never_executes_a_second_action(command_state, change):
    service, sessions, ids, action = command_state
    first = service.execute(context(ids), request())
    retry = request("Different") if change == "parameters" else request()
    if change == "scope":
        retry = retry.model_copy(update={"scope": AccessScope(type="node", id="node-a")})
    if change == "command":
        other = RenameFixture()
        other.definition = action.definition.model_copy(update={"name": "profile.other"})
        service = CommandService(sessions, [action, other])
        retry = retry.model_copy(update={"command": "profile.other"})
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), retry)
    assert error.value.code == ("PERMISSION_DENIED" if change == "scope" else "CONFLICT")
    assert action.calls == 1
    assert counts(sessions) == (1, 1)
    assert service.execute(context(ids), request()).command_id == first.command_id


def test_identical_key_is_independent_for_distinct_authenticated_actors(command_state):
    service, sessions, ids, action = command_state
    first = service.execute(context(ids), request("A"))
    second = service.execute(context(ids, user="bob"), request("B"))
    assert first.command_id != second.command_id and not second.duplicate
    assert action.calls == 2 and counts(sessions) == (2, 2)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "A"
        assert db.get(User, ids["bob"]).display_name == "B"


@pytest.mark.parametrize(
    "claimed,target,allowed",
    [("node-a", "node-b", False), ("node-b", "node-b", False), ("node-a", "node-a", True)],
)
def test_scope_is_derived_from_the_actual_target_before_permission_check(
    command_state, claimed, target, allowed
):
    _, sessions, ids, _ = command_state

    class ScopedInput(RenameParameters):
        node_id: str = Field(min_length=1, max_length=128)

    class ScopedRename(RenameFixture):
        def normalize(self, parameters):
            return ScopedInput.model_validate(parameters).model_dump(mode="json")

        def authorization_scope(self, session, parameters):
            return AccessScope(type="node", id=parameters["node_id"])

    with sessions.begin() as db:
        binding = db.scalars(select(RoleBinding).where(RoleBinding.user_id == ids["alice"])).one()
        binding.scope_type, binding.scope_id = "node", "node-a"
    action = ScopedRename()
    service = CommandService(sessions, [action])
    query = CommandRequest(
        command="profile.rename",
        scope=AccessScope(type="node", id=claimed),
        parameters={"name": "Scoped", "node_id": target},
    )
    if allowed:
        assert not service.execute(context(ids), query).duplicate
        assert action.calls == 1 and counts(sessions) == (1, 1)
    else:
        with pytest.raises(DomainError) as error:
            service.execute(context(ids), query)
        assert error.value.code == "PERMISSION_DENIED"
        assert action.calls == 0 and counts(sessions) == (0, 0)


@pytest.mark.parametrize("revocation", ["binding", "permission", "suspension"])
def test_replay_rechecks_authority_in_the_command_transaction(command_state, revocation):
    service, sessions, ids, action = command_state
    service.execute(context(ids), request())
    with sessions.begin() as db:
        if revocation == "binding":
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        elif revocation == "permission":
            db.execute(delete(RolePermission))
        else:
            db.get(User, ids["alice"]).status = "suspended"
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), request())
    assert error.value.code == "PERMISSION_DENIED"
    assert action.calls == 1 and counts(sessions) == (1, 1)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"name": " "},
        {"name": "x", "requires_confirmation": False},
        {"name": "private-marker", "unexpected": True},
        {"name": "x" * 17000},
        {"name": float("nan")},
    ],
)
def test_invalid_parameters_fail_before_any_database_business_write(command_state, parameters):
    service, sessions, ids, action = command_state
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), request().model_copy(update={"parameters": parameters}))
    assert error.value.code == "VALIDATION_ERROR"
    assert "private-marker" not in str(error.value)
    assert action.calls == 0 and counts(sessions) == (0, 0)


@pytest.mark.parametrize(
    "bad_result", [{"data": "x" * 66000}, {"data": float("inf")}, {"data": object()}]
)
def test_invalid_result_rolls_back_business_claim_and_audit(command_state, bad_result):
    service, sessions, ids, action = command_state
    action.bad_result = bad_result
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), request())
    assert error.value.code == "INTERNAL_ERROR" and counts(sessions) == (0, 0)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"


def test_business_failure_is_atomic_and_allows_a_clean_retry(command_state):
    service, sessions, ids, action = command_state
    action.fail = True
    with pytest.raises(RuntimeError):
        service.execute(context(ids), request())
    assert counts(sessions) == (0, 0)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"
    action.fail = False
    assert not service.execute(context(ids), request()).duplicate
    assert counts(sessions) == (1, 1) and action.calls == 2


def test_action_cannot_accidentally_commit_business_work_outside_result_and_audit(command_state):
    _, sessions, ids, _ = command_state

    class EarlyCommit(RenameFixture):
        def execute(self, session, execution, principal, parameters):
            result = super().execute(session, execution, principal, parameters)
            session.commit()
            return result

    with pytest.raises(DomainError) as error:
        CommandService(sessions, [EarlyCommit()]).execute(context(ids), request())
    assert error.value.code == "INTERNAL_ERROR" and counts(sessions) == (0, 0)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"


def test_action_can_release_a_savepoint_without_committing_the_owner_transaction(command_state):
    _, sessions, ids, _ = command_state

    class NestedRename(RenameFixture):
        def execute(self, session, execution, principal, parameters):
            with session.begin_nested():
                return super().execute(session, execution, principal, parameters)

    result = CommandService(sessions, [NestedRename()]).execute(context(ids), request())
    assert result.result == {"name": "Renamed"} and counts(sessions) == (1, 1)


def test_session_commit_inside_savepoint_cannot_commit_the_owner_early(command_state):
    _, sessions, ids, _ = command_state

    class NestedEarlyCommit(RenameFixture):
        def execute(self, session, execution, principal, parameters):
            with session.begin_nested():
                result = super().execute(session, execution, principal, parameters)
                session.commit()
                return result

    with pytest.raises(DomainError) as error:
        CommandService(sessions, [NestedEarlyCommit()]).execute(context(ids), request())
    assert error.value.code == "INTERNAL_ERROR" and counts(sessions) == (0, 0)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"


def test_confirmation_is_required_by_default_and_cannot_be_overridden_by_parameters(command_state):
    _, sessions, ids, action = command_state
    action.definition = CommandDefinition(
        name="profile.rename", required_permission="profile.write"
    )
    service = CommandService(sessions, [action])
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), request())
    assert error.value.code == "CONFIRMATION_REQUIRED"
    assert action.calls == 0 and counts(sessions) == (0, 0)


def test_unregistered_and_duplicate_registrations_do_not_enable_arbitrary_actions(command_state):
    _, sessions, ids, action = command_state
    with pytest.raises(DomainError) as error:
        CommandService(sessions).execute(context(ids), request())
    assert error.value.code == "VALIDATION_ERROR"
    with pytest.raises(ValueError, match="duplicate command registration"):
        CommandService(sessions, [action, action])
    assert action.calls == 0 and counts(sessions) == (0, 0)


def test_committed_incomplete_record_is_not_automatically_reexecuted(command_state):
    service, sessions, ids, action = command_state
    first = service.execute(context(ids), request())
    with sessions.begin() as db:
        record = db.get(CommandRecord, str(first.command_id))
        record.state, record.result = "pending", None
    with pytest.raises(DomainError) as error:
        service.execute(context(ids), request())
    assert error.value.code == "CONFLICT" and action.calls == 1
    assert counts(sessions) == (1, 1)


def test_abrupt_fixture_exit_before_commit_rolls_back_then_original_key_succeeds(
    command_state, settings
):
    service, sessions, ids, action = command_state
    program = textwrap.dedent("""
        import json, os, sys
        from aris.core.db import build_engine, session_factory
        from aris.core.commands.service import CommandService
        from aris.core.commands.schemas import CommandContext, CommandRequest
        from tests.test_commands import RenameFixture

        class BlockingRename(RenameFixture):
            def execute(self, session, execution, principal, parameters):
                result = super().execute(session, execution, principal, parameters)
                session.flush()
                print(json.dumps({'phase': 'business_flushed_before_commit'}), flush=True)
                sys.stdin.read()
                return result

        engine = build_engine(os.environ['ARIS_COMMAND_TEST_DATABASE_URL'])
        service = CommandService(session_factory(engine), [BlockingRename()])
        data = json.loads(os.environ['ARIS_COMMAND_TEST_INPUT'])
        service.execute(CommandContext.model_validate(data['context']),
                        CommandRequest.model_validate(data['request']))
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "ARIS_COMMAND_TEST_DATABASE_URL": settings.database_url,
            "ARIS_COMMAND_TEST_INPUT": json.dumps(
                {
                    "context": context(ids).model_dump(mode="json"),
                    "request": request().model_dump(mode="json"),
                }
            ),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "fixture command did not reach its crash point"
        line = child.stdout.readline()
        assert line and json.loads(line)["phase"] == "business_flushed_before_commit"
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    assert counts(sessions) == (0, 0)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"
    assert not service.execute(context(ids), request()).duplicate
    assert counts(sessions) == (1, 1) and action.calls == 1


@pytest.mark.parametrize("conflicting", [False, True])
def test_postgres_concurrent_keys_commit_only_one_business_write(command_state, conflicting):
    service, sessions, ids, action = command_state
    with sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL conflict waiting and row locks")
    start = Barrier(2)

    def submit(index):
        start.wait(timeout=10)
        try:
            return service.execute(
                context(ids, request_id=f"request-{index}"),
                request(f"Name-{index}" if conflicting else "Name"),
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert action.calls == 1 and counts(sessions) == (1, 1)
    if conflicting:
        assert outcomes.count("CONFLICT") == 1
        assert len([item for item in outcomes if item != "CONFLICT"]) == 1
    else:
        assert sorted(item.duplicate for item in outcomes) == [False, True]
        assert len({item.command_id for item in outcomes}) == 1
