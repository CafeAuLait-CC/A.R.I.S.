import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from aris.apps.api.main import create_app
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.models import CommandIntakeReceipt, CommandJob, CommandRecord
from aris.core.commands.queue_schemas import DurableCommandContext
from aris.core.commands.worker import CommandWorker
from aris.interfaces.slack.client import SlackDeliveryError
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.user_commands import (
    CONFIRM_ACTION,
    STATUS_ACTION,
    WITHDRAW_ACTION,
    SlackCommandAdapter,
)
from tests.test_command_intake import LIMITS, WORKER_LIMITS
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state
from tests.test_commands import request
from tests.test_slack import SIGNING_SECRET, signed
from tests.test_slack import slack_settings as slack_settings
from tests.test_user_commands import LIMITS as GATEWAY_LIMITS


class FakeModal:
    def __init__(self, sessions):
        self.sessions, self.updates, self.states, self.failure = sessions, [], [], None

    def open_modal(self, trigger, view):
        raise AssertionError("command callback must update only its existing modal")

    def update_modal(self, handle, view):
        with self.sessions() as session:
            self.states.append(list(session.scalars(select(CommandRecord.state))))
        self.updates.append((handle, view))
        if self.failure:
            raise self.failure


@pytest.fixture
def slack_commands(durable_state, settings, slack_settings):
    previous, sessions, ids, action, clock = durable_state
    with sessions.begin() as session:
        for identity in session.scalars(select(ExternalIdentity)):
            identity.namespace = "T111"
            identity.external_id = "U111" if identity.user_id == ids["alice"] else "U222"
    intake = CommandIntakeService(
        previous.commands,
        IdentityNamespace(provider="slack", namespace="T111"),
        LIMITS,
        confirmation_key_id="fixture-key-1",
    )
    gateway = UserCommandGateway(intake, ["profile.rename"], GATEWAY_LIMITS)
    modal = FakeModal(sessions)
    adapter = SlackCommandAdapter(gateway, modal)
    app = create_app(settings, slack_settings, slack_commands=adapter)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, intake, sessions, ids, action, clock, modal


def pending(state):
    _, intake, _, ids, _, _, _ = state
    result = intake.submit(
        DurableCommandContext(
            actor_id=ids["alice"],
            subject={"provider": "slack", "namespace": "T111", "external_id": "U111"},
            request_id="fixture-submit",
            idempotency_key="private-intent-key",
            receipt_key="private-submit-receipt",
        ),
        request(),
    )
    assert result.disposition == "accepted"
    return result.outcome


def payload(waiting, *, action=CONFIRM_ACTION, user="U111"):
    value = {"command_id": str(waiting.command_id)}
    if action == CONFIRM_ACTION:
        value["proof"] = waiting.proof.get_secret_value()
    return {
        "type": "block_actions",
        "team": {"id": "T111"},
        "user": {"id": user},
        "actions": [{"action_id": action, "value": json.dumps(value)}],
        "view": {
            "id": "V111",
            "type": "modal",
            "callback_id": "aris.command.confirmation",
            "hash": "fixture-hash",
        },
        "response_url": "https://private.invalid/do-not-follow",
        "trigger_id": "private-trigger-never-store",
    }


def send(client, data, **header_changes):
    body = urlencode({"payload": json.dumps(data)}).encode()
    return client.post(
        "/slack/events",
        content=body,
        headers={
            **signed(body, "application/x-www-form-urlencoded"),
            "X-Request-ID": "slack-command-test",
            **header_changes,
        },
    )


def record_counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (CommandIntakeReceipt, CommandJob, SlackReceipt, SlackHomeJob)
        )


def test_signed_confirmation_acks_persisted_queue_then_updates_modal_without_execution(
    slack_commands,
):
    client, intake, sessions, _, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    data = payload(waiting)
    first = send(client, data)
    assert first.status_code == 200 and first.json() == {}
    assert action.calls == 0 and modal.states == [["queued"]]
    assert record_counts(sessions) == (2, 1, 0, 0)
    text = json.dumps(modal.updates[-1][1])
    assert "Confirmed and queued" in text and "completed" not in text
    assert waiting.proof.get_secret_value() not in text and "private-trigger" not in text
    assert send(client, data).status_code == 200
    assert record_counts(sessions) == (2, 1, 0, 0)
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    assert send(client, data).status_code == 200 and action.calls == 1
    assert "has completed" in json.dumps(modal.updates[-1][1])


@pytest.mark.parametrize(
    "who", ["other_user", "unmapped_user", "other_workspace", "bad_signature", "expired_signature"]
)
def test_signed_source_still_requires_current_owner_identity_and_workspace(slack_commands, who):
    client, _, sessions, _, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    data, changes = payload(waiting), {}
    if who == "other_user":
        data["user"]["id"] = "U222"
    elif who == "unmapped_user":
        data["user"]["id"] = "U999"
    elif who == "other_workspace":
        data["team"]["id"] = "T999"
    elif who == "bad_signature":
        changes["X-Slack-Signature"] = "v0=" + "0" * 64
    else:
        changes["X-Slack-Request-Timestamp"] = "1"
    response = send(client, data, **changes)
    if who in {"other_user", "unmapped_user"}:
        assert response.status_code == 200
        assert "not authorized" in json.dumps(modal.updates[-1][1])
    else:
        assert response.status_code == (403 if who == "other_workspace" else 401)
        assert not modal.updates
    with sessions() as session:
        assert session.get(CommandRecord, str(waiting.command_id)).state == "awaiting_confirmation"
    assert action.calls == 0 and record_counts(sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize(
    "bad",
    [
        "actor_in_value",
        "multiple_actions",
        "message_source",
        "wrong_callback",
        "bad_value",
        "unsupported_action",
        "submission",
    ],
)
def test_callback_value_and_modal_context_cannot_select_actor_or_bypass_validation(
    slack_commands, bad
):
    client, _, sessions, ids, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    data = payload(waiting)
    if bad == "actor_in_value":
        value = json.loads(data["actions"][0]["value"])
        value["actor_id"] = ids["bob"]
        data["actions"][0]["value"] = json.dumps(value)
    elif bad == "multiple_actions":
        data["actions"].append({"action_id": "refresh_home"})
    elif bad == "message_source":
        data.pop("view")
    elif bad == "wrong_callback":
        data["view"]["callback_id"] = "different-view"
    elif bad == "bad_value":
        data["actions"][0]["value"] = "private-invalid-marker"
    elif bad == "unsupported_action":
        data["actions"][0]["action_id"] = "aris.command.arbitrary"
    else:
        data["type"] = "view_submission"
    response = send(client, data)
    assert response.status_code == 422 and not modal.updates and action.calls == 0
    assert (
        waiting.proof.get_secret_value() not in response.text
        and "private-invalid" not in response.text
    )
    assert record_counts(sessions) == (1, 0, 0, 0)


def test_old_intent_after_permission_revocation_is_not_confirmed_but_can_be_withdrawn(
    slack_commands,
):
    client, _, sessions, ids, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    with sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    assert send(client, payload(waiting)).status_code == 200
    assert "not authorized" in json.dumps(modal.updates[-1][1])
    assert send(client, payload(waiting, action=WITHDRAW_ACTION)).status_code == 200
    assert "unexecuted command was withdrawn" in json.dumps(modal.updates[-1][1])
    with sessions() as session:
        assert session.get(CommandRecord, str(waiting.command_id)).state == "cancelled"
    assert action.calls == 0


def test_expired_queue_is_not_presented_as_a_verified_terminal_outcome(slack_commands):
    client, intake, _, _, action, clock, modal = slack_commands
    waiting = pending(slack_commands)
    send(client, payload(waiting))
    clock["now"] += timedelta(seconds=LIMITS.max_queue_delay_seconds)
    assert send(client, payload(waiting, action=STATUS_ACTION)).status_code == 200
    assert "final result is still being checked" in json.dumps(modal.updates[-1][1])
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "expired"
    assert send(client, payload(waiting, action=STATUS_ACTION)).status_code == 200
    assert "confirmation or execution deadline" in json.dumps(modal.updates[-1][1])
    assert action.calls == 0


def test_modal_update_failure_does_not_roll_back_accepted_command_or_expose_private_body(
    slack_commands, caplog
):
    client, intake, sessions, _, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    modal.failure = SlackDeliveryError("hash_conflict", retryable=False)
    response = send(client, payload(waiting))
    assert response.status_code == 200 and record_counts(sessions) == (2, 1, 0, 0)
    assert "command_modal_update_failed" in caplog.text
    assert all(
        marker not in caplog.text
        for marker in (
            waiting.proof.get_secret_value(),
            "private-trigger",
            "private.invalid",
            SIGNING_SECRET,
        )
    )
    assert (
        CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded" and action.calls == 1
    )


def test_default_application_does_not_enable_signed_write_callbacks(
    slack_commands, settings, slack_settings
):
    _, _, sessions, _, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    with TestClient(create_app(settings, slack_settings), raise_server_exceptions=False) as client:
        response = send(client, payload(waiting))
    assert response.status_code == 503 and not modal.updates and action.calls == 0
    assert record_counts(sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("header", ["X-Slack-Signature", "X-Slack-Request-Timestamp"])
def test_ambiguous_signed_headers_do_not_reach_command_gateway(slack_commands, header):
    client, _, sessions, _, action, _, modal = slack_commands
    waiting = pending(slack_commands)
    body = urlencode({"payload": json.dumps(payload(waiting))}).encode()
    supplied = list(signed(body, "application/x-www-form-urlencoded").items())
    supplied.append((header, "v0=" + "0" * 64 if header.endswith("Signature") else "1"))
    response = client.post("/slack/events", content=body, headers=supplied)
    assert response.status_code == 401 and not modal.updates and action.calls == 0
    assert record_counts(sessions) == (1, 0, 0, 0)
