import json
from time import monotonic
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from sqlalchemy import delete

from aris.core.access.models import RoleBinding
from aris.interfaces.slack import usage_modal
from aris.interfaces.slack.client import SlackClient, SlackDeliveryError
from aris.interfaces.slack.home import block_cost, home_view
from aris.interfaces.slack.schemas import Interaction, ModalHandle
from aris.interfaces.slack.usage_contracts import (
    USAGE_CALLBACK,
    USAGE_CONTROLS,
    USAGE_HOME_ACTION,
    USAGE_NODE_ACTION,
    USAGE_PERIOD_ACTION,
    USAGE_REFRESH_ACTION,
)
from aris.interfaces.slack.usage_modal import UsageModalAdapter
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_slack import signed, slack_client, slack_settings
from tests.test_slack_home_sample import snapshot
from tests.test_slack_usage import queue_counts, usage_slack
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import counts, ledger

__all__ = [
    "admin_state",
    "history_state",
    "derivations",
    "ledger",
    "readings",
    "slack_settings",
    "usage_slack",
    "slack_client",
]


def open_payload(**changes):
    return {
        "type": "block_actions",
        "team": {"id": "TFIXTURE"},
        "user": {"id": "UFIRST"},
        "trigger_id": "fixture-modal-trigger",
        "actions": [{"action_id": USAGE_HOME_ACTION}],
        **changes,
    }


def send(client, payload):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )


def selection_payload(view, action, value=None):
    binding = json.loads(view["private_metadata"])
    current = {USAGE_NODE_ACTION: binding["node_id"], USAGE_PERIOD_ACTION: binding["period"]}
    if value is not None:
        current[action] = value
    item = {"action_id": action}
    if value is not None:
        item["selected_option"] = {"value": value}
    return open_payload(
        view={**view, "id": "VUSAGE", "hash": "hash-current", "team_id": "TFIXTURE"},
        actions=[item],
        state={
            "values": {
                USAGE_CONTROLS: {
                    id: {"selected_option": {"value": val}} for id, val in current.items()
                }
            }
        },
    )


@pytest.fixture
def modal(usage_slack, monkeypatch):
    client, queries, readings, _ = usage_slack
    calls = []
    updates = []

    def opened(self, trigger, view):
        calls.append("open")
        assert trigger == "fixture-modal-trigger"
        assert view["blocks"][0]["text"]["text"] == "Loading your recorded usage…"
        return ModalHandle("VUSAGE", "hash-open")

    def updated(self, handle, view):
        calls.append("update")
        updates.append((handle, view))

    monkeypatch.setattr(SlackClient, "open_modal", opened)
    monkeypatch.setattr(SlackClient, "update_modal", updated)
    monkeypatch.setattr(usage_modal, "monotonic", lambda: 0)
    return client, queries, readings, calls, updates


def test_usage_modal_opens_before_queries_and_switches_current_nodes_and_periods_without_writes(
    modal, monkeypatch
):
    client, queries, readings, calls, updates = modal
    state, (_, actor, _, ids), _ = readings
    with queries.sessions.begin() as session:
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(actor.actor_id),
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-b",
            )
        )
    original = queries.choices

    def observed(context):
        calls.append("choices")
        return original(context)

    monkeypatch.setattr(queries, "choices", observed)
    before, queues = counts(state), queue_counts(queries)
    response = send(client, open_payload())
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert calls[:2] == ["open", "choices"] and calls[-1] == "update"
    assert "30 GPU seconds recorded" in json.dumps(updates[-1][1])
    handle, view = updates[-1]
    assert handle.hash == "hash-open" and view["callback_id"] == USAGE_CALLBACK
    assert json.loads(view["private_metadata"])["user_id"] == "UFIRST"
    changed = send(client, selection_payload(view, USAGE_NODE_ACTION, "node-b"))
    assert changed.status_code == 200 and updates[-1][0].hash == "hash-current"
    assert "Actual usage remains unknown" in json.dumps(updates[-1][1])
    changed = send(client, selection_payload(updates[-1][1], USAGE_PERIOD_ACTION, "today"))
    assert (
        changed.status_code == 200
        and json.loads(updates[-1][1]["private_metadata"])["period"] == "today"
    )
    assert send(client, selection_payload(updates[-1][1], USAGE_REFRESH_ACTION)).status_code == 200
    assert counts(state) == before and queue_counts(queries) == queues


def test_usage_modal_binding_missing_hash_and_ambiguous_state_are_rejected_before_delivery(modal):
    client, queries, _, calls, updates = modal
    assert send(client, open_payload()).status_code == 200
    view = updates[-1][1]
    baseline = len(updates)
    changed = selection_payload(view, USAGE_REFRESH_ACTION)
    changed["user"] = {"id": "USECOND"}
    assert send(client, changed).status_code == 403
    changed = selection_payload(view, USAGE_REFRESH_ACTION)
    changed["view"]["id"] = "VOTHER"
    assert send(client, changed).status_code == 403
    changed = selection_payload(view, USAGE_REFRESH_ACTION)
    changed["view"].pop("hash")
    assert send(client, changed).status_code == 422
    changed = selection_payload(view, USAGE_NODE_ACTION, "node-b")
    changed["state"]["values"][USAGE_CONTROLS][USAGE_NODE_ACTION]["selected_option"]["value"] = (
        "node-a"
    )
    assert send(client, changed).status_code == 422
    changed = selection_payload(view, USAGE_NODE_ACTION, "node-a")
    changed["actions"][0]["selected_option"] = None
    assert send(client, changed).status_code == 422
    assert len(updates) == baseline and calls.count("open") == 1


def test_usage_modal_current_revocation_and_display_conflict_do_not_publish_fake_empty(
    modal, monkeypatch, caplog
):
    client, queries, readings, calls, updates = modal
    assert send(client, open_payload()).status_code == 200
    view = updates[-1][1]
    attempts = []
    original_update = SlackClient.update_modal

    def conflict(self, handle, value):
        attempts.append(handle)
        raise SlackDeliveryError("hash_conflict", retryable=False)

    monkeypatch.setattr(SlackClient, "update_modal", conflict)
    assert send(client, selection_payload(view, USAGE_REFRESH_ACTION)).status_code == 200
    assert len(attempts) == 1 and len(updates) == 1 and "hash_conflict" in caplog.text
    assert "fixture-modal-trigger" not in caplog.text
    monkeypatch.setattr(SlackClient, "update_modal", original_update)
    owner = readings[1][1].actor_id
    with queries.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == str(owner)))
    assert send(client, selection_payload(view, USAGE_REFRESH_ACTION)).status_code == 200
    result = json.dumps(updates[-1][1])
    assert (
        "not authorized" in result
        and "0 GPU seconds" not in result
        and "30 GPU seconds" not in result
    )


def test_usage_callback_ack_does_not_query_or_consume_trigger_before_background(modal, monkeypatch):
    _, queries, _, calls, updates = modal
    publisher = SlackClient.__new__(SlackClient)
    adapter = UsageModalAdapter(queries, publisher)
    payload = open_payload()
    interaction = Interaction.model_validate(payload)
    original = queries.choices

    def observed(context):
        calls.append("choices")
        return original(context)

    monkeypatch.setattr(queries, "choices", observed)
    reply = adapter.interact(interaction, payload, "ack-only", monotonic())
    assert reply.payload == {"ok": True} and not calls and reply.after_ack is not None
    reply.after_ack()
    assert calls[:2] == ["open", "choices"] and updates
    before = len(calls)
    monkeypatch.setattr(usage_modal, "monotonic", lambda: 10**15)
    expired = adapter.interact(interaction, payload, "expired-trigger", 0)
    expired.after_ack()
    assert len(calls) == before


def test_optional_home_usage_footer_keeps_native_cards_and_block_budget(slack_client, settings):
    data = snapshot(slack_client, settings, gpu_count=3)
    baseline = home_view(data)
    enabled = home_view(data, usage_available=True)
    assert enabled["blocks"][:-2] == baseline["blocks"][:-1]
    assert enabled["blocks"][-1] == baseline["blocks"][-1]
    assert enabled["blocks"][-2]["elements"][0]["action_id"] == USAGE_HOME_ACTION
    entry = data.nodes[0]
    gpus = [
        entry.node.gpus[0].model_copy(update={"index": index, "gpu_uuid": f"GPU-many-{index}"})
        for index in range(16)
    ]
    crowded = data.model_copy(
        update={
            "nodes": [
                entry.model_copy(update={"node": entry.node.model_copy(update={"gpus": gpus})})
            ]
        }
    )
    view = home_view(crowded, usage_available=True)
    assert sum(block_cost(block) for block in view["blocks"]) <= 100
    assert "My usage" in json.dumps(view)
