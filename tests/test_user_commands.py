import json
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select, text

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.access.schemas import AccessScope
from aris.core.audit import AuditEntry
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import ExternalCommandSubmit, GatewayLimits
from aris.core.commands.models import CommandIntakeReceipt, CommandRecord
from aris.core.commands.queue_repo import CommandQueueRepository
from aris.core.commands.worker import CommandWorker
from aris.core.config import AgentCredential
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.slack.config import SlackSettings
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_command_intake import WORKER_LIMITS, subject
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state

ALICE_TOKEN = "fixture-user-api-alice-credential-1234567890"
BOB_TOKEN = "fixture-user-api-bob-credential-123456789012"
LIMITS = GatewayLimits(lock_timeout_ms=50, statement_timeout_ms=150, transaction_timeout_ms=250)


@pytest.fixture
def user_api(durable_state, settings):
    intake, sessions, ids, action, clock = durable_state
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),
            UserTokenBinding(subject=subject("bob"), token=SecretStr(BOB_TOKEN)),
        ],
        reserved_tokens=[SecretStr(READ_TOKEN), SecretStr(AGENT_TOKEN)],
    )
    gateway = UserCommandGateway(intake, ["profile.rename"], LIMITS)
    surface = UserCommandSurface(gateway, auth)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), user_commands=surface),
        raise_server_exceptions=False,
    ) as client:
        yield client, intake, sessions, ids, action, clock, surface


def headers(*, token=ALICE_TOKEN, key="fixture-intent"):
    return {
        "Authorization": "Bearer " + token,
        "Idempotency-Key": key,
        "X-Request-ID": "fixture-user-request",
    }


def submit(client, *, token=ALICE_TOKEN, key="fixture-intent", name="Renamed"):
    return client.post(
        "/internal/commands",
        headers=headers(token=token, key=key),
        json={"command": "profile.rename", "parameters": {"name": name}},
    )


def queue(client):
    first = submit(client)
    assert first.status_code == 202, first.text
    outcome = first.json()["outcome"]
    accepted = client.post(
        f"/internal/commands/{outcome['command_id']}/confirm",
        headers=headers(),
        json={"proof": outcome["proof"]},
    )
    assert accepted.status_code == 202, accepted.text
    return first.json(), accepted.json()


def command_count(sessions):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(CommandRecord))


def test_default_app_has_no_user_command_routes(client):
    assert not any(route.path.startswith("/internal/commands") for route in client.app.routes)
    assert client.post("/internal/commands", json={}).status_code == 404


def test_authenticated_http_intent_confirmation_worker_and_status_are_distinct(user_api):
    client, intake, sessions, ids, action, _, _ = user_api
    pending, queued = queue(client)
    first = pending["outcome"]
    assert len(first["proof"]) == 64 and "*" not in first["proof"]
    assert first["state"] == "awaiting_confirmation" and queued["outcome"]["state"] == "queued"
    assert action.calls == 0 and command_count(sessions) == 1
    with sessions() as session:
        record = session.get(CommandRecord, first["command_id"])
        assert record.actor_id == ids["alice"] and record.scope_type == "global"
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    result = client.get(f"/internal/commands/{first['command_id']}", headers=headers())
    assert result.status_code == 200 and result.json()["state"] == "succeeded"
    assert result.json()["result"] == {"name": "Renamed"} and action.calls == 1
    assert result.headers["Cache-Control"] == "no-store"
    assert ALICE_TOKEN not in result.text


@pytest.mark.parametrize("token", [READ_TOKEN, AGENT_TOKEN, "wrong-credential"])
def test_read_and_node_credentials_never_authenticate_user_commands(user_api, token):
    client, _, sessions, _, action, _, _ = user_api
    rejected = submit(client, token=token)
    assert rejected.status_code == 401 and rejected.json()["error"]["code"] == "PERMISSION_DENIED"
    assert command_count(sessions) == 0 and action.calls == 0


@pytest.mark.parametrize("field", ["actor_id", "owner_id", "scope"])
def test_json_cannot_select_actor_owner_or_scope(user_api, field):
    client, _, sessions, ids, action, _, _ = user_api
    request = {
        "command": "profile.rename",
        "parameters": {"name": "Changed"},
        field: {"type": "global", "id": "*"} if field == "scope" else ids["bob"],
    }
    result = client.post("/internal/commands", headers=headers(), json=request)
    assert result.status_code == 422 and command_count(sessions) == 0 and action.calls == 0
    nested = client.post(
        "/internal/commands",
        headers=headers(),
        json={
            "command": "profile.rename",
            "parameters": {"name": "Changed", field: ids["bob"]},
        },
    )
    assert nested.status_code == 422 and command_count(sessions) == 0
    assert ids["bob"] not in nested.text


def test_actual_scope_is_derived_from_registered_action(user_api):
    client, _, sessions, _, action, _, _ = user_api
    action.authorization_scope = lambda *_: AccessScope(type="node", id="node-a")
    result = submit(client)
    assert result.status_code == 202
    with sessions() as session:
        record = session.scalar(select(CommandRecord))
        assert (record.scope_type, record.scope_id) == ("node", "node-a")


def test_another_authenticated_user_cannot_confirm_withdraw_or_query_original_intent(user_api):
    client, _, sessions, _, action, _, _ = user_api
    pending = submit(client).json()["outcome"]
    url = "/internal/commands/" + pending["command_id"]
    results = [
        client.get(url, headers=headers(token=BOB_TOKEN)),
        client.post(
            url + "/confirm", headers=headers(token=BOB_TOKEN), json={"proof": pending["proof"]}
        ),
        client.post(url + "/withdraw", headers=headers(token=BOB_TOKEN), json={}),
    ]
    assert all(result.status_code == 403 for result in results)
    assert all("Renamed" not in result.text for result in results)
    assert command_count(sessions) == 1 and action.calls == 0


@pytest.mark.parametrize("phase", ["confirmation", "worker"])
def test_current_grant_revocation_blocks_old_authenticated_intent(user_api, phase):
    client, intake, sessions, ids, action, _, _ = user_api
    if phase == "worker":
        _, accepted = queue(client)
        command_id = accepted["outcome"]["command_id"]
    else:
        pending = submit(client).json()["outcome"]
        command_id = pending["command_id"]
    with sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    if phase == "confirmation":
        result = client.post(
            f"/internal/commands/{command_id}/confirm",
            headers=headers(),
            json={"proof": pending["proof"]},
        )
        assert result.status_code == 403 and result.json()["disposition"] == "rejected"
    else:
        assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "failed"
    assert action.calls == 0


def test_changed_external_mapping_does_not_transfer_old_command_to_new_owner(user_api):
    client, _, sessions, ids, action, _, _ = user_api
    pending = submit(client).json()["outcome"]
    with sessions.begin() as session:
        session.scalar(
            select(ExternalIdentity).where(ExternalIdentity.external_id == "U_alice")
        ).user_id = ids["bob"]
    denied = client.post(
        f"/internal/commands/{pending['command_id']}/confirm",
        headers=headers(),
        json={"proof": pending["proof"]},
    )
    assert denied.status_code == 403 and action.calls == 0


def test_retries_reuse_original_deadline_and_different_content_conflicts(user_api):
    client, _, sessions, _, action, clock, _ = user_api
    first = submit(client)
    clock["now"] += timedelta(seconds=30)
    again = submit(client, name=" Renamed ")
    assert again.status_code == 202 and again.json()["duplicate"]
    assert again.json()["outcome"] == first.json()["outcome"]
    assert first.headers["Cache-Control"] == "no-store"
    conflict = submit(client, name="Other")
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "CONFLICT"
    assert command_count(sessions) == 1 and action.calls == 0


def test_queued_operation_can_be_withdrawn_but_executed_business_cannot_be_undone(user_api):
    client, intake, sessions, _, action, _, _ = user_api
    _, accepted = queue(client)
    command_id = accepted["outcome"]["command_id"]
    withdrawn = client.post(f"/internal/commands/{command_id}/withdraw", headers=headers(), json={})
    assert withdrawn.status_code == 200 and withdrawn.json()["outcome"]["state"] == "cancelled"
    assert CommandWorker(intake, WORKER_LIMITS).run_once() is None and action.calls == 0
    new = submit(client, key="second").json()["outcome"]
    client.post(
        f"/internal/commands/{new['command_id']}/confirm",
        headers=headers(key="second"),
        json={"proof": new["proof"]},
    )
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    refused = client.post(
        f"/internal/commands/{new['command_id']}/withdraw", headers=headers(key="second"), json={}
    )
    assert refused.status_code == 409 and refused.json()["error_code"] == "COMMAND_ALREADY_FINISHED"


@pytest.mark.parametrize("bad", ["large", "not_json", "malformed", "missing_key"])
def test_http_ingress_is_bounded_and_rejects_invalid_input_without_business(user_api, bad):
    client, _, sessions, _, action, _, _ = user_api
    if bad == "large":
        response = client.post(
            "/internal/commands",
            headers={**headers(), "Content-Type": "application/json"},
            content=b"x" * 32769,
        )
        assert response.status_code == 413
    elif bad == "not_json":
        response = client.post(
            "/internal/commands", headers=headers(), content="private-input-marker"
        )
        assert response.status_code == 415
    elif bad == "malformed":
        response = client.post(
            "/internal/commands",
            headers={**headers(), "Content-Type": "application/json"},
            content=b'{"private-input-marker"',
        )
        assert response.status_code == 422
    else:
        response = client.post(
            "/internal/commands",
            headers={"Authorization": "Bearer " + ALICE_TOKEN},
            json={"command": "profile.rename", "parameters": {"name": "Changed"}},
        )
        assert response.status_code == 422
    assert "private-input-marker" not in response.text and ALICE_TOKEN not in response.text
    assert command_count(sessions) == 0 and action.calls == 0


def test_absent_current_subject_is_denied_without_automatic_registration(user_api):
    client, _, sessions, ids, _, _, _ = user_api
    with sessions.begin() as session:
        session.execute(delete(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"]))
    result = submit(client)
    assert result.status_code == 403 and command_count(sessions) == 0
    with sessions() as session:
        audit = session.scalar(
            select(AuditEntry).where(AuditEntry.action == "command.gateway.rejected")
        )
        assert audit.actor.startswith("external:") and audit.changes == {
            "code": "PERMISSION_DENIED"
        }
        assert "U_alice" not in json.dumps(audit.changes)


def test_unknown_and_unexposed_commands_cannot_use_the_user_surface(user_api):
    client, _, sessions, _, action, _, _ = user_api
    result = client.post(
        "/internal/commands", headers=headers(), json={"command": "events.replay", "parameters": {}}
    )
    assert result.status_code == 403 and command_count(sessions) == 0 and action.calls == 0


def test_user_credential_binding_rejects_reuse_and_hides_secrets():
    binding = UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN))
    assert ALICE_TOKEN not in repr(binding)
    with pytest.raises(ValueError, match="reserved"):
        BoundSubjectTokens([binding], reserved_tokens=[SecretStr(ALICE_TOKEN)])
    with pytest.raises(ValueError, match="duplicate"):
        BoundSubjectTokens([binding, binding], reserved_tokens=[])
    with pytest.raises(ValueError, match="required"):
        BoundSubjectTokens([], reserved_tokens=[])
    with pytest.raises(ValidationError):
        UserTokenBinding(subject=subject(), token="short")
    auth = BoundSubjectTokens([binding], reserved_tokens=[])
    for raw in (None, "", "Basic " + ALICE_TOKEN, "Bearer  " + ALICE_TOKEN):
        with pytest.raises(DomainError) as denied:
            auth.authenticate(raw)
        assert denied.value.status == 401 and ALICE_TOKEN not in str(denied.value)


def test_nonexistent_target_command_is_generic_and_does_not_create_intent(user_api):
    client, _, sessions, _, action, _, _ = user_api
    response = client.get(f"/internal/commands/{uuid4()}", headers=headers())
    assert response.status_code == 403 and command_count(sessions) == 0 and action.calls == 0


@pytest.mark.parametrize("bad", ["sql_write", "commit"])
def test_gateway_scope_provider_cannot_commit_or_write_during_metadata_resolution(user_api, bad):
    client, _, sessions, ids, action, _, _ = user_api

    def invalid_scope(session, parameters):
        if bad == "sql_write":
            session.execute(text("UPDATE users SET display_name='Illicit'"))
        else:
            session.commit()
        return AccessScope(type="global", id="*")

    action.authorization_scope = invalid_scope
    result = submit(client)
    assert result.status_code == 500 and result.json()["error"]["code"] == "INTERNAL_ERROR"
    assert command_count(sessions) == 0 and action.calls == 0
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"


def test_postgres_gateway_scope_timeout_never_reports_admission_or_executes_business(user_api):
    client, _, sessions, _, action, _, _ = user_api
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL metadata statement budget")
    original = action.authorization_scope

    def slow(session, parameters):
        session.execute(text("SELECT pg_sleep(2)"))
        return original(session, parameters)

    action.authorization_scope = slow
    started = time.monotonic()
    response = submit(client)
    assert response.status_code == 503 and response.json()["error"]["code"] == "GATEWAY_UNAVAILABLE"
    assert time.monotonic() - started < 2 and command_count(sessions) == 0 and action.calls == 0
    assert response.headers["Cache-Control"] == "no-store"
    action.authorization_scope = original
    assert submit(client).status_code == 202


@pytest.mark.parametrize("kind", ["read", "node", "production"])
def test_application_refuses_reused_interface_keys_and_development_auth_in_production(
    user_api, settings, monkeypatch, kind
):
    _, _, _, _, _, _, surface = user_api
    if kind == "read":
        config = settings.model_copy(update={"read_token": SecretStr(ALICE_TOKEN)})
    elif kind == "node":
        config = settings.model_copy(
            update={
                "agent_credentials": {
                    "fixture-node": AgentCredential(node_id="node-a", token=SecretStr(ALICE_TOKEN))
                }
            }
        )
    else:
        config = settings.model_copy(
            update={
                "environment": "production",
                "database_url": "postgresql+psycopg://unused.invalid/never_connected",
            }
        )
    built = []
    monkeypatch.setattr("aris.apps.api.main.build_engine", lambda url: built.append(url))
    with pytest.raises(ValueError) as refused:
        create_app(config, SlackSettings(_env_file=None), user_commands=surface)
    assert not built and ALICE_TOKEN not in str(refused.value)
    assert ("production" if kind == "production" else "reserved") in str(refused.value)


@pytest.mark.parametrize("ambiguous", ["authorization", "idempotency-key"])
def test_ambiguous_authentication_or_intent_headers_are_rejected(user_api, ambiguous):
    client, _, sessions, _, action, _, _ = user_api
    supplied = [("Authorization", "Bearer " + ALICE_TOKEN), ("Idempotency-Key", "fixture-key")]
    supplied.append(
        ("Authorization", "Bearer " + BOB_TOKEN)
        if ambiguous == "authorization"
        else ("Idempotency-Key", "different-key")
    )
    response = client.post(
        "/internal/commands",
        headers=supplied,
        json={"command": "profile.rename", "parameters": {"name": "Changed"}},
    )
    assert response.status_code == (401 if ambiguous == "authorization" else 422)
    assert response.headers["Cache-Control"] == "no-store"
    assert command_count(sessions) == 0 and action.calls == 0


def recovery_context(**changes):
    return ExternalCommandSubmit(
        **{
            "subject": subject(),
            "request_id": "fixture-recovery",
            "receipt_key": "recovery-does-not-admit",
            "idempotency_key": "fixture-intent",
            **changes,
        }
    )


def test_key_recovery_preserves_canonical_intent_proof_deadlines_and_receipts(user_api):
    client, _, sessions, _, action, _, surface = user_api
    gateway = surface.gateway
    assert gateway.definition("profile.rename").requires_confirmation
    assert gateway.recover_by_key(recovery_context()) is None
    first = submit(client).json()["outcome"]
    with sessions() as session:
        receipts = session.scalar(select(func.count()).select_from(CommandIntakeReceipt))
    recovered = gateway.recover_by_key(recovery_context())
    assert str(recovered.command_id) == first["command_id"]
    assert recovered.request.parameters == {"name": "Renamed"}
    assert recovered.outcome.model_dump(mode="json", exclude={"proof"}) == {
        k: v for k, v in first.items() if k != "proof"
    }
    assert recovered.outcome.proof.get_secret_value() == first["proof"]
    assert (
        action.calls == 0
        and gateway.recover_by_key(recovery_context(subject=subject("bob"))) is None
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandIntakeReceipt)) == receipts


@pytest.mark.parametrize("change", ["namespace", "revoked", "suspended", "remapped", "key"])
def test_key_recovery_rechecks_current_identity_role_and_confirmation_material(user_api, change):
    client, intake, sessions, ids, _, _, surface = user_api
    submit(client)
    context = recovery_context()
    with sessions.begin() as session:
        if change == "revoked":
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        elif change == "suspended":
            session.get(User, ids["alice"]).status = "suspended"
        elif change == "remapped":
            identity = session.scalar(
                select(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"])
            )
            identity.user_id = ids["bob"]
    if change == "namespace":
        context = recovery_context(subject=subject().model_copy(update={"namespace": "T_OTHER"}))
    if change == "key":
        intake.confirmation_key_id = "rotated-fixture"
    if change == "remapped":
        # A new principal has no command under the old principal's key.
        assert surface.gateway.recover_by_key(context) is None
    else:
        with pytest.raises(DomainError) as rejected:
            surface.gateway.recover_by_key(context)
        assert rejected.value.code == (
            "CONFIRMATION_KEY_CHANGED" if change == "key" else "PERMISSION_DENIED"
        )


def test_key_recovery_respects_current_allowlist_and_does_not_renew_expiry(user_api):
    client, _, sessions, _, _, clock, surface = user_api
    submit(client)
    review = surface.gateway.recover_by_key(recovery_context())
    clock["now"] = review.outcome.expires_at + timedelta(seconds=1)
    expired = surface.gateway.recover_by_key(recovery_context())
    assert expired.outcome.state == "expired" and expired.command_id == review.command_id
    with sessions() as session:
        record = session.get(CommandRecord, str(expired.command_id))
        assert utc(record.confirmation_expires_at) == utc(review.outcome.expires_at)
    surface.gateway.allowed = frozenset()
    with pytest.raises(DomainError) as refused:
        surface.gateway.recover_by_key(recovery_context())
    assert refused.value.code == "PERMISSION_DENIED"


def test_postgres_key_recovery_has_a_bounded_lookup_and_never_creates_receipts(
    user_api, monkeypatch
):
    _, _, sessions, _, action, _, surface = user_api
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL recovery lookup statement budget")

    def slow(repo, actor_id, key):
        repo.session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(CommandQueueRepository, "by_key", slow)
    started = time.monotonic()
    with pytest.raises(DomainError) as unavailable:
        surface.gateway.recover_by_key(recovery_context())
    assert unavailable.value.code == "GATEWAY_UNAVAILABLE"
    assert time.monotonic() - started < 2 and action.calls == 0
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandIntakeReceipt)) == 0
