import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, text

from aris.core.access.models import RoleBinding, User
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import (
    ActorContext,
    CommandDefinition,
    CommandResult,
    ConfirmCommand,
    ExpiredCommand,
    PendingConfirmation,
)
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from tests.test_commands import command_state, context, counts, request

__all__ = ["command_state"]
KEY = SecretStr("test-only-confirmation-signing-key-12345")


@pytest.fixture
def confirmed_state(command_state):
    _, sessions, ids, action = command_state
    action.definition = CommandDefinition(
        name="profile.rename", required_permission="profile.write"
    )
    clock = {"now": datetime(2026, 11, 16, tzinfo=UTC)}
    service = CommandService(sessions, [action], confirmation_key=KEY, clock=lambda: clock["now"])
    return service, sessions, ids, action, clock


def actor(ids, user="alice", request_id="confirm-request"):
    return ActorContext(actor_id=ids[user], request_id=request_id)


def confirmation(pending):
    assert isinstance(pending, PendingConfirmation)
    return ConfirmCommand(command_id=pending.command_id, proof=pending.proof)


def test_waiting_intent_does_not_execute_or_extend_ttl_on_retry_and_survives_restart(
    confirmed_state,
):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())
    assert isinstance(pending, PendingConfirmation) and pending.state == "awaiting_confirmation"
    assert pending.expires_at == clock["now"] + timedelta(seconds=300)
    assert action.calls == 0 and counts(sessions) == (1, 1)
    assert pending.proof.get_secret_value() not in repr(pending)
    clock["now"] += timedelta(seconds=100)
    restarted = CommandService(sessions, [action], confirmation_key=KEY, clock=lambda: clock["now"])
    again = restarted.execute(context(ids, request_id="request-retry"), request())
    assert again == pending and counts(sessions) == (1, 1)
    with sessions() as db:
        assert db.get(User, ids["alice"]).display_name == "Original A"


def test_confirmation_commits_once_and_valid_duplicates_return_original_result(confirmed_state):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())
    result = service.confirm(actor(ids), confirmation(pending))
    assert (
        isinstance(result, CommandResult) and result.state == "succeeded" and not result.duplicate
    )
    assert result.result == {"name": "Renamed"} and result.command_id == pending.command_id
    assert action.calls == 1 and counts(sessions) == (1, 3)
    clock["now"] = pending.expires_at + timedelta(seconds=100)
    duplicate = service.confirm(actor(ids, request_id="confirm-retry"), confirmation(pending))
    resubmitted = service.execute(context(ids), request())
    assert duplicate.duplicate and resubmitted.duplicate
    assert duplicate.command_id == result.command_id and duplicate.result == result.result
    assert action.calls == 1 and counts(sessions) == (1, 3)


@pytest.mark.parametrize("wrong", ["actor", "proof", "different_intent"])
def test_confirmation_cannot_move_between_actors_or_intents(confirmed_state, wrong):
    service, sessions, ids, action, _ = confirmed_state
    pending = service.execute(context(ids), request())
    proof, who = confirmation(pending), actor(ids)
    if wrong == "actor":
        who = actor(ids, user="bob")
    elif wrong == "proof":
        proof = ConfirmCommand(command_id=pending.command_id, proof="0" * 64)
    else:
        other = service.execute(context(ids, key="another-key"), request("Another"))
        proof = ConfirmCommand(command_id=other.command_id, proof=pending.proof)
    with pytest.raises(DomainError) as error:
        service.confirm(who, proof)
    assert error.value.code == "PERMISSION_DENIED" and action.calls == 0
    assert counts(sessions) == ((2, 2) if wrong == "different_intent" else (1, 1))


def test_confirm_request_cannot_supply_changed_parameters_or_scope(confirmed_state):
    service, sessions, ids, action, _ = confirmed_state
    pending = service.execute(context(ids), request())
    with pytest.raises(ValidationError):
        ConfirmCommand(
            command_id=pending.command_id, proof=pending.proof, parameters={"name": "Hacked"}
        )
    assert action.calls == 0 and counts(sessions) == (1, 1)


@pytest.mark.parametrize("field", ["parameters", "scope"])
def test_changed_stored_intent_cannot_use_the_original_confirmation(confirmed_state, field):
    service, sessions, ids, action, _ = confirmed_state
    pending = service.execute(context(ids), request())
    with sessions.begin() as db:
        record = db.get(CommandRecord, str(pending.command_id))
        if field == "parameters":
            record.parameters = {"name": "Hacked"}
        else:
            record.scope_type, record.scope_id = "node", "node-b"
    with pytest.raises(DomainError) as error:
        service.confirm(actor(ids), confirmation(pending))
    assert error.value.code == ("PERMISSION_DENIED" if field == "scope" else "CONFLICT")
    assert action.calls == 0
    assert counts(sessions) == (1, 1)


def test_expiry_is_persisted_once_at_the_boundary_and_never_executes(confirmed_state):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())
    clock["now"] = pending.expires_at
    expired = service.confirm(actor(ids), confirmation(pending))
    assert isinstance(expired, ExpiredCommand) and expired.command_id == pending.command_id
    assert isinstance(service.confirm(actor(ids), confirmation(pending)), ExpiredCommand)
    assert isinstance(service.execute(context(ids), request()), ExpiredCommand)
    assert action.calls == 0 and counts(sessions) == (1, 2)
    with sessions() as db:
        assert db.get(CommandRecord, str(pending.command_id)).state == "expired"
        assert db.get(User, ids["alice"]).display_name == "Original A"


@pytest.mark.parametrize("completed", [False, True])
def test_revocation_prevents_confirmation_and_access_to_a_prior_success(confirmed_state, completed):
    service, sessions, ids, action, _ = confirmed_state
    pending = service.execute(context(ids), request())
    if completed:
        service.confirm(actor(ids), confirmation(pending))
    with sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    with pytest.raises(DomainError) as error:
        service.confirm(actor(ids), confirmation(pending))
    assert error.value.code == "PERMISSION_DENIED"
    assert action.calls == int(completed)
    assert counts(sessions) == ((1, 3) if completed else (1, 1))


def test_failed_confirmation_rolls_back_confirmed_audit_and_business_before_retry(confirmed_state):
    service, sessions, ids, action, _ = confirmed_state
    pending = service.execute(context(ids), request())
    action.fail = True
    with pytest.raises(RuntimeError):
        service.confirm(actor(ids), confirmation(pending))
    assert counts(sessions) == (1, 1)
    with sessions() as db:
        assert db.get(CommandRecord, str(pending.command_id)).state == "awaiting_confirmation"
        assert db.get(User, ids["alice"]).display_name == "Original A"
    action.fail = False
    assert not service.confirm(actor(ids), confirmation(pending)).duplicate
    assert action.calls == 2 and counts(sessions) == (1, 3)


def test_changed_normalization_cannot_reinterpret_a_previous_confirmation(confirmed_state):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())

    class Reinterpreted(type(action)):
        def normalize(self, parameters):
            return {"name": "Changed semantic meaning"}

    changed = Reinterpreted()
    changed.definition = action.definition
    newer = CommandService(sessions, [changed], confirmation_key=KEY, clock=lambda: clock["now"])
    with pytest.raises(DomainError) as error:
        newer.confirm(actor(ids), confirmation(pending))
    assert error.value.code == "CONFLICT" and changed.calls == 0
    assert counts(sessions) == (1, 1)


def test_abrupt_confirmation_fixture_exit_restores_waiting_intent_and_allows_retry(
    confirmed_state, settings
):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())
    program = textwrap.dedent("""
        import json, os, sys
        from datetime import datetime
        from aris.core.db import build_engine, session_factory
        from aris.core.commands.schemas import ActorContext, CommandDefinition, ConfirmCommand
        from aris.core.commands.service import CommandService
        from tests.test_commands import RenameFixture
        from tests.test_confirmations import KEY

        class BlockingRename(RenameFixture):
            definition = CommandDefinition(
                name='profile.rename', required_permission='profile.write')
            def execute(self, session, principal, parameters):
                result = super().execute(session, principal, parameters)
                session.flush()
                print(json.dumps({'phase': 'confirmation_business_flushed'}), flush=True)
                sys.stdin.read()
                return result

        data = json.loads(os.environ['ARIS_CONFIRM_TEST_INPUT'])
        engine = build_engine(os.environ['ARIS_CONFIRM_TEST_DATABASE_URL'])
        now = datetime.fromisoformat(data['now'])
        service = CommandService(session_factory(engine), [BlockingRename()],
                                 confirmation_key=KEY, clock=lambda: now)
        service.confirm(ActorContext.model_validate(data['actor']),
                        ConfirmCommand.model_validate(data['confirmation']))
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "ARIS_CONFIRM_TEST_DATABASE_URL": settings.database_url,
            "ARIS_CONFIRM_TEST_INPUT": json.dumps(
                {
                    "now": clock["now"].isoformat(),
                    "actor": actor(ids).model_dump(mode="json"),
                    "confirmation": {
                        "command_id": str(pending.command_id),
                        "proof": pending.proof.get_secret_value(),
                    },
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
            assert ready.select(timeout=10), "confirmation fixture did not reach its crash point"
        line = child.stdout.readline()
        assert line and json.loads(line)["phase"] == "confirmation_business_flushed"
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    assert counts(sessions) == (1, 1)
    with sessions() as db:
        assert db.get(CommandRecord, str(pending.command_id)).state == "awaiting_confirmation"
        assert db.get(User, ids["alice"]).display_name == "Original A"
    assert not service.confirm(actor(ids), confirmation(pending)).duplicate
    assert action.calls == 1 and counts(sessions) == (1, 3)


def test_missing_or_rotated_key_never_bypasses_confirmation_or_refreshes_expiry(confirmed_state):
    service, sessions, ids, action, clock = confirmed_state
    pending = service.execute(context(ids), request())
    no_key = CommandService(sessions, [action], clock=lambda: clock["now"])
    with pytest.raises(DomainError) as error:
        no_key.confirm(actor(ids), confirmation(pending))
    assert error.value.code == "CONFIRMATION_REQUIRED" and action.calls == 0
    rotated = CommandService(
        sessions,
        [action],
        confirmation_key=SecretStr("different-test-key-1234567890-1234567890"),
        clock=lambda: clock["now"],
    )
    with pytest.raises(DomainError) as error:
        rotated.confirm(actor(ids), confirmation(pending))
    assert error.value.code == "PERMISSION_DENIED"
    renewed = rotated.execute(context(ids), request())
    assert renewed.command_id == pending.command_id and renewed.expires_at == pending.expires_at
    assert renewed.proof != pending.proof and counts(sessions) == (1, 1)
    assert not rotated.confirm(actor(ids), confirmation(renewed)).duplicate


def test_missing_key_after_definition_change_cannot_execute_an_existing_waiting_intent(
    confirmed_state,
):
    service, sessions, ids, action, clock = confirmed_state
    service.execute(context(ids), request())
    action.definition = action.definition.model_copy(update={"requires_confirmation": False})
    no_key = CommandService(sessions, [action], clock=lambda: clock["now"])
    with pytest.raises(DomainError) as error:
        no_key.execute(context(ids), request())
    assert error.value.code == "CONFIRMATION_REQUIRED" and action.calls == 0
    assert counts(sessions) == (1, 1)


def test_postgres_simultaneous_confirmations_only_execute_once(confirmed_state):
    service, sessions, ids, action, _ = confirmed_state
    with sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL confirmation row locking")
    pending = service.execute(context(ids), request())
    start = Barrier(2)

    def confirm(index):
        start.wait(timeout=10)
        return service.confirm(actor(ids, request_id=f"confirm-{index}"), confirmation(pending))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(confirm, range(2)))
    assert sorted(item.duplicate for item in outcomes) == [False, True]
    assert len({item.command_id for item in outcomes}) == 1
    assert action.calls == 1 and counts(sessions) == (1, 3)


def test_0008_preserves_preexisting_low_risk_command_results_on_round_trip(command_state):
    service, sessions, ids, _ = command_state
    service.execute(context(ids), request())

    def snapshot():
        with sessions() as db:
            return sorted(
                json.dumps(dict(row._mapping), sort_keys=True, default=str)
                for row in db.execute(
                    text(
                        "SELECT id,actor_id,key_sha256,command_type,scope_type,scope_id,"
                        "fingerprint,parameters,state,result,created_at,completed_at FROM commands"
                    )
                )
            )

    before = snapshot()
    config = Config("alembic.ini")
    command.downgrade(config, "0007")
    assert snapshot() == before
    command.upgrade(config, "head")
    command.check(config)
    assert snapshot() == before


def test_0008_downgrade_refuses_to_destroy_waiting_or_completed_confirmation_history(
    confirmed_state,
):
    service, sessions, ids, _, _ = confirmed_state
    pending = service.execute(context(ids), request())
    for completed in (False, True):
        if completed:
            service.confirm(actor(ids), confirmation(pending))
        with pytest.raises(RuntimeError, match="confirmation history prevents downgrade"):
            command.downgrade(Config("alembic.ini"), "0007")
        with sessions() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        command.check(Config("alembic.ini"))
    assert counts(sessions) == (1, 3)
