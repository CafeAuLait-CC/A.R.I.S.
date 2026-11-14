from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aris.core.audit import AuditEntry
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.models import AgentMessage, Node
from tests.conftest import AGENT_TOKEN
from tests.test_telemetry import batch, post, status


def heartbeat(seq=1, seconds_ago=0, **changes):
    payload = batch(seq=seq, seconds_ago=seconds_ago)
    del payload["gpus"]
    return {
        **payload,
        "schema_version": "aris.heartbeat/v1",
        "collector_status": "ok",
        "capabilities": ["gpu.telemetry/v1", "agent.heartbeat/v1"],
        **changes,
    }


def beat(client, payload, token=AGENT_TOKEN):
    return client.post("/vega/v1/heartbeat", json=payload, headers={"X-Agent-Token": token})


def test_heartbeat_registers_alive_node_without_inventing_gpu_facts(client):
    assert beat(client, heartbeat(collector_status="error")).status_code == 200
    node = status(client)["nodes"][0]
    assert node["status"] == "ONLINE"
    assert node["collector_status"] == "error"
    assert node["telemetry_status"] == "MISSING"
    assert node["observed_at"] is None
    assert node["received_at"] is None
    assert node["heartbeat_at"] == node["last_seen_at"]
    assert node["gpus"] == []
    with Session(client.app.state.engine) as session:
        for model in (AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1
        assert session.scalars(select(DomainEvent)).one().event_type == "vega.heartbeat.accepted"


def test_heartbeat_does_not_refresh_old_samples_or_hide_collector_failure(client):
    assert post(client, batch(seconds_ago=90)).status_code == 200
    assert beat(client, heartbeat(seq=2)).status_code == 200
    node = status(client)["nodes"][0]
    assert node["status"] == "ONLINE"
    assert node["telemetry_status"] == "STALE"
    assert node["gpus"][0]["observed_state"] == "unknown"
    assert post(client, batch(seq=3)).status_code == 200
    assert beat(client, heartbeat(seq=4, collector_status="error")).status_code == 200
    node = status(client)["nodes"][0]
    assert node["telemetry_status"] == "ERROR"
    assert node["gpus"][0]["observed_state"] == "unknown"
    # Recovery requires new facts, not just a healthy heartbeat over stale facts.
    assert post(client, batch(seq=5)).status_code == 200
    assert beat(client, heartbeat(seq=6)).status_code == 200
    assert status(client)["nodes"][0]["gpus"][0]["observed_state"] == "idle"


@pytest.mark.parametrize("age,health", [(90, "STALE"), (300, "OFFLINE")])
def test_delayed_heartbeat_never_revives_old_node(client, age, health):
    assert beat(client, heartbeat(seconds_ago=age)).status_code == 200
    assert status(client)["nodes"][0]["status"] == health


def test_heartbeat_and_samples_have_independent_projection_sequences(client):
    newer = heartbeat(seq=3)
    assert beat(client, newer).json()["applied_to_current"] is True
    assert post(client, batch(seq=2, seconds_ago=1)).json()["applied_to_current"] is True
    old = heartbeat(seq=1, seconds_ago=120, collector_status="error")
    assert beat(client, old).json()["applied_to_current"] is False
    node = status(client)["nodes"][0]
    assert node["seq"] == 3
    assert node["collector_status"] == "ok"
    assert datetime.fromisoformat(node["heartbeat_at"]) == datetime.fromisoformat(
        newer["observed_at"]
    )
    assert node["gpus"][0]["observed_state"] == "idle"


def test_heartbeat_auth_validation_and_global_message_identity(client):
    assert beat(client, heartbeat(), "wrong").status_code == 401
    assert beat(client, heartbeat(node_id="node-b")).status_code == 403
    assert beat(client, heartbeat(collector_status="invented")).status_code == 422
    assert beat(client, heartbeat(observed_at="2026-01-01T00:00:00")).status_code == 422
    original = heartbeat()
    assert beat(client, original).json()["duplicate"] is False
    assert beat(client, original).json()["duplicate"] is True
    assert beat(client, {**original, "collector_status": "error"}).status_code == 409
    assert post(client, batch(message_id=original["message_id"], seq=2)).status_code == 409
    assert post(client, batch(seq=1)).status_code == 409
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentMessage)) == 1


def test_postgres_heartbeat_and_sample_concurrent_first_reports(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locking")
    start = Barrier(2)
    calls = [lambda: beat(client, heartbeat(seq=2)), lambda: post(client, batch())]

    def submit(call):
        start.wait(timeout=10)
        return call()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, calls))
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["applied_to_current"] for response in responses)
    node = status(client)["nodes"][0]
    assert node["heartbeat_at"] is not None
    assert node["observed_at"] is not None
    assert node["gpus"][0]["observed_state"] == "idle"
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(Node)) == 1
        for model in (AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 2


def test_upgrade_preserves_existing_telemetry_and_replay(client):
    original = batch()
    assert post(client, original).status_code == 200
    before = status(client)["nodes"][0]
    command.downgrade(Config("alembic.ini"), "0001")
    assert client.get("/health/ready").status_code == 503
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    after = status(client)["nodes"][0]
    assert after["observed_at"] == before["observed_at"]
    assert after["gpus"] == before["gpus"]
    assert post(client, original).json()["duplicate"] is True
    assert beat(client, heartbeat(seq=2)).status_code == 200
    with client.app.state.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
