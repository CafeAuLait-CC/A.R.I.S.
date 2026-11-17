import time
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select, update

from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandIntakeReceipt, CommandJob, CommandRecord
from aris.core.commands.queue_schemas import DurableActorContext
from aris.core.commands.schemas import CommandReview
from aris.core.commands.worker import CommandWorker
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_command_intake import WORKER_LIMITS, subject
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state
from tests.test_user_commands import ALICE_TOKEN, BOB_TOKEN, headers, queue, submit
from tests.test_user_commands import user_api as user_api


def path(command_id):
    return f"/internal/commands/{command_id}/review"


def persisted_counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (CommandRecord, CommandIntakeReceipt, CommandJob, AuditEntry)
        )


def test_owner_can_recover_normalized_intent_and_original_proof_without_new_admission(user_api):
    client, intake, sessions, _, action, clock, surface = user_api
    first = submit(client, name="  Private canonical intent  ").json()["outcome"]
    before = persisted_counts(sessions)
    clock["now"] += timedelta(seconds=3)
    response = client.get(path(first["command_id"]), headers=headers())
    assert response.status_code == 200, response.text
    review = response.json()
    assert review["request"] == {
        "command": "profile.rename",
        "scope": {"type": "global", "id": "*"},
        "parameters": {"name": "Private canonical intent"},
    }
    assert review["command_id"] == first["command_id"] and review["outcome"] == first
    assert response.headers["Cache-Control"] == "no-store"
    repeated = client.get(path(first["command_id"]), headers=headers())
    assert repeated.json() == review and persisted_counts(sessions) == before and action.calls == 0
    typed = surface.review("Bearer " + ALICE_TOKEN, "fixture-review", UUID(first["command_id"]))
    assert first["proof"] not in repr(typed) and "Private canonical intent" not in repr(typed)
    with pytest.raises(ValidationError):
        CommandReview(command_id=uuid4(), request=typed.request, outcome=typed.outcome)
    assert (
        intake.inspect(intake_context(user_api), UUID(first["command_id"])).command_id
        == typed.command_id
    )


def intake_context(state):
    return DurableActorContext(
        actor_id=state[3]["alice"], subject=subject(), request_id="fixture-review"
    )


def test_reviewed_proof_runs_original_command_once_and_retains_intent_after_success(user_api):
    client, intake, sessions, _, action, _, _ = user_api
    pending = submit(client).json()["outcome"]
    review = client.get(path(pending["command_id"]), headers=headers()).json()
    accepted = client.post(
        f"/internal/commands/{pending['command_id']}/confirm",
        headers=headers(),
        json={"proof": review["outcome"]["proof"]},
    )
    assert accepted.status_code == 202
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    before = persisted_counts(sessions)
    completed = client.get(path(pending["command_id"]), headers=headers())
    assert completed.status_code == 200
    assert completed.json()["outcome"]["state"] == "succeeded"
    assert completed.json()["request"] == review["request"]
    assert "proof" not in completed.json()["outcome"] and action.calls == 1
    assert persisted_counts(sessions) == before


@pytest.mark.parametrize("token,status", [(BOB_TOKEN, 403), (READ_TOKEN, 401), (AGENT_TOKEN, 401)])
def test_private_intent_and_confirmation_proof_are_unavailable_to_other_credentials(
    user_api, token, status
):
    client, _, sessions, _, action, _, _ = user_api
    pending = submit(client, name="Private canonical intent").json()["outcome"]
    before = persisted_counts(sessions)[:3]
    response = client.get(path(pending["command_id"]), headers=headers(token=token))
    assert response.status_code == status
    assert "Private canonical intent" not in response.text and pending["proof"] not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert persisted_counts(sessions)[:3] == before and action.calls == 0


@pytest.mark.parametrize("change", ["mapping", "permission", "allowlist"])
def test_review_rechecks_current_identity_grants_and_exposed_command_list(user_api, change):
    client, _, sessions, ids, _, _, surface = user_api
    pending = submit(client).json()["outcome"]
    with sessions.begin() as session:
        if change == "mapping":
            session.execute(
                update(ExternalIdentity)
                .where(ExternalIdentity.user_id == ids["alice"])
                .values(user_id=ids["bob"])
            )
        elif change == "permission":
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        else:
            surface.gateway.allowed = frozenset()
    response = client.get(path(pending["command_id"]), headers=headers())
    assert response.status_code == 403 and "Renamed" not in response.text
    assert pending["proof"] not in response.text


def test_review_never_resigns_a_pending_intent_after_key_rotation(user_api):
    client, intake, _, _, _, _, _ = user_api
    pending = submit(client).json()["outcome"]
    intake.commands.confirmation_key = SecretStr("fixture-new-independent-key-12345678901234567890")
    response = client.get(path(pending["command_id"]), headers=headers())
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "CONFIRMATION_KEY_CHANGED"
    )
    assert pending["proof"] not in response.text


@pytest.mark.parametrize("change", ["parameters", "definition", "scope"])
def test_review_does_not_expose_unverified_persistent_intent_or_changed_definition(
    user_api, change
):
    client, intake, sessions, _, action, _, _ = user_api
    pending = submit(client).json()["outcome"]
    with sessions.begin() as session:
        if change == "parameters":
            session.execute(
                update(CommandRecord)
                .where(CommandRecord.id == pending["command_id"])
                .values(parameters={"name": "Unverified content"})
            )
        elif change == "scope":
            session.execute(
                update(CommandRecord)
                .where(CommandRecord.id == pending["command_id"])
                .values(scope_type="node", scope_id="node-a")
            )
        else:
            definition = action.definition.model_copy(update={"requires_confirmation": False})
            intake.commands.commands["profile.rename"] = (definition, action)
    response = client.get(path(pending["command_id"]), headers=headers())
    assert response.status_code == (403 if change == "scope" else 409)
    assert "Unverified content" not in response.text and pending["proof"] not in response.text


def test_review_returns_canonical_parameters_even_if_equivalent_storage_is_not_normalized(user_api):
    client, _, sessions, _, _, _, _ = user_api
    pending = submit(client).json()["outcome"]
    with sessions.begin() as session:
        session.execute(
            update(CommandRecord)
            .where(CommandRecord.id == pending["command_id"])
            .values(parameters={"name": "  Renamed  "})
        )
    response = client.get(path(pending["command_id"]), headers=headers())
    assert response.status_code == 200
    assert response.json()["request"]["parameters"] == {"name": "Renamed"}
    assert response.json()["outcome"]["proof"] == pending["proof"]


def test_expiry_during_review_is_recorded_once_without_renewing_original_bounds(user_api):
    client, _, sessions, _, action, clock, _ = user_api
    pending = submit(client).json()["outcome"]
    clock["now"] = datetime.fromisoformat(pending["expires_at"]) + timedelta(seconds=1)
    response = client.get(path(pending["command_id"]), headers=headers())
    assert response.status_code == 200 and response.json()["outcome"]["state"] == "expired"
    before = persisted_counts(sessions)
    assert "proof" not in response.json()["outcome"]
    assert client.get(path(pending["command_id"]), headers=headers()).json() == response.json()
    assert persisted_counts(sessions) == before and action.calls == 0
    with sessions() as session:
        record = session.get(CommandRecord, pending["command_id"])
        assert record.state == "expired"
        assert record.confirmation_expires_at.replace(
            tzinfo=clock["now"].tzinfo
        ) == datetime.fromisoformat(pending["expires_at"])
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEntry)
                .where(AuditEntry.action == "command.expired")
            )
            == 1
        )


def test_queued_review_distinguishes_elapsed_deadline_from_worker_reconciled_expiry(user_api):
    client, intake, sessions, _, _, clock, _ = user_api
    _, accepted = queue(client)
    queued = accepted["outcome"]
    clock["now"] = datetime.fromisoformat(queued["execute_by"]) + timedelta(seconds=1)
    response = client.get(path(queued["command_id"]), headers=headers())
    assert response.status_code == 200 and response.json()["outcome"]["state"] == "queued"
    assert response.json()["outcome"]["deadline_elapsed"]
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "expired"
    reconciled = client.get(path(queued["command_id"]), headers=headers())
    assert reconciled.status_code == 200 and reconciled.json()["outcome"]["state"] == "expired"


def test_unknown_or_ambiguous_owner_authentication_never_reveals_review(user_api):
    client, _, _, _, _, _, _ = user_api
    assert client.get(path(uuid4()), headers=headers()).status_code == 403
    pending = submit(client).json()["outcome"]
    response = client.get(
        path(pending["command_id"]),
        headers=[
            ("Authorization", "Bearer " + ALICE_TOKEN),
            ("Authorization", "Bearer " + BOB_TOKEN),
        ],
    )
    assert response.status_code == 401 and pending["proof"] not in response.text


def test_review_does_not_wait_indefinitely_on_an_inflight_command_lock(user_api):
    client, _, sessions, _, _, _, _ = user_api
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL review lock budget")
    pending = submit(client).json()["outcome"]
    with sessions.begin() as blocker:
        blocker.execute(
            select(CommandRecord).where(CommandRecord.id == pending["command_id"]).with_for_update()
        ).one()
        started = time.monotonic()
        response = client.get(path(pending["command_id"]), headers=headers())
        assert time.monotonic() - started < 1.5
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "INTAKE_UNAVAILABLE"
