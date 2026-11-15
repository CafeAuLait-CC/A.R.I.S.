"""Credential maintenance must preserve durable identity and exact retry semantics."""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aris.apps.agent.spool import Spool
from aris.apps.api.main import create_app
from aris.core.config import Settings
from tests.conftest import AGENT_TOKEN
from tests.resource_fixtures import resource_observation
from tests.test_live import hello
from tests.test_telemetry import batch, post, status

NEW_TOKEN = "rotated-test-agent-token-12345"


def reconfigure(settings, change):
    values = settings.model_dump()
    change(values["agent_credentials"])
    return Settings(_env_file=None, **values)


def assert_live_denied(client, token):
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(
            "/vega/live?agent_id=agent-a&node_id=node-a", headers={"X-Agent-Token": token}
        ):
            pytest.fail("retired credential upgraded to a WebSocket")
    assert closed.value.code == 1008


def test_rotation_after_lost_ack_replays_original_message_under_new_token(settings, tmp_path):
    path = tmp_path / "original.sqlite3"
    spool = Spool(path, "agent-a", "node-a")
    original = spool.enqueue_resource("node-a", resource_observation())
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM pending").fetchone()[0]
    inode = path.stat().st_ino
    with TestClient(create_app(settings)) as old:
        # The server commits but the Collector never sees/acknowledges this response.
        assert post(old, original.model_dump(mode="json")).json()["duplicate"] is False

    rotated = reconfigure(settings, lambda items: items["agent-a"].update(token=NEW_TOKEN))
    with TestClient(create_app(rotated)) as new:
        assert post(new, original.model_dump(mode="json")).status_code == 401
        assert_live_denied(new, AGENT_TOKEN)
        restarted = Spool(path, "agent-a", "node-a")
        retry = restarted.pending()[0]
        receipt = post(new, retry.model_dump(mode="json"), NEW_TOKEN)
        assert receipt.status_code == 200 and receipt.json()["duplicate"] is True
        assert path.stat().st_ino == inode
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT payload FROM pending").fetchone()[0] == raw
        restarted.acknowledge(retry)
        assert restarted.pending() == []
        next_report = restarted.enqueue_resource("node-a", resource_observation())
        assert next_report.seq == original.seq + 1
        assert post(new, next_report.model_dump(mode="json"), NEW_TOKEN).status_code == 200
        with new.websocket_connect(
            "/vega/live?agent_id=agent-a&node_id=node-a", headers={"X-Agent-Token": NEW_TOKEN}
        ) as socket:
            socket.send_json(hello().model_dump(mode="json"))
            assert socket.receive_json()["lease_seconds"] == 0
        # Other nodes retain their independent credential and registration path.
        other = batch(agent_id="agent-b", node_id="node-b", gpus=[])
        assert post(new, other, "second-agent-credential-12345").status_code == 200
        assert {node["node_id"] for node in status(new)["nodes"]} == {"node-a", "node-b"}


def test_revocation_rejects_http_and_new_live_sessions_without_erasing_facts(settings):
    with TestClient(create_app(settings)) as old:
        assert post(old, batch()).status_code == 200
    revoked = reconfigure(settings, lambda items: items.pop("agent-a"))
    with TestClient(create_app(revoked)) as new:
        assert post(new, batch(seq=2)).status_code == 401
        assert_live_denied(new, AGENT_TOKEN)
        assert status(new)["nodes"][0]["seq"] == 1
        assert status(new)["nodes"][0]["agent_id"] == "agent-a"


def test_empty_spool_does_not_silently_reset_an_existing_sequence(client, tmp_path):
    assert post(client, batch()).status_code == 200
    replacement = Spool(tmp_path / "lost-original.sqlite3", "agent-a", "node-a")
    attempted = replacement.enqueue_resource("node-a", resource_observation())
    assert post(client, attempted.model_dump(mode="json")).status_code == 409
    assert replacement.pending() == [attempted]
    assert status(client)["nodes"][0]["seq"] == 1


def test_configuration_change_cannot_rebind_an_existing_node(settings):
    with TestClient(create_app(settings)) as old:
        assert post(old, batch()).status_code == 200

    def replace_identity(items):
        credential = items.pop("agent-a")
        items["replacement-agent"] = credential

    replaced = reconfigure(settings, replace_identity)
    with TestClient(create_app(replaced)) as new:
        report = batch(seq=2, agent_id="replacement-agent")
        assert post(new, report).status_code == 403
        assert status(new)["nodes"][0]["agent_id"] == "agent-a"
