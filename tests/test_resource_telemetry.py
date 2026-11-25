import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.main import flush
from aris.apps.agent.spool import Spool
from aris.core.audit import AuditEntry
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.models import AgentMessage, Node, NodeResource
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.resource_fixtures import resource_observation
from tests.test_agent import samples
from tests.test_heartbeat import beat, heartbeat
from tests.test_telemetry import batch, post, status


def resource_batch(seq=1, **changes):
    resources = resource_observation(**changes)
    envelope = batch(seq=seq)
    return {
        **envelope,
        "observed_at": resources.observed_at.isoformat(),
        "resources": resources.model_dump(mode="json"),
    }


def put(client, payload, token=AGENT_TOKEN):
    return client.post("/vega/telemetry/batch", json=payload, headers={"X-Agent-Token": token})


def resources(client):
    response = client.get("/public/resources", headers={"Authorization": "Bearer " + READ_TOKEN})
    assert response.status_code == 200
    return response.json()


def test_components_are_one_atomic_message_with_gpu_and_resource_views(client):
    payload = resource_batch()
    assert put(client, payload, token="wrong").status_code == 401
    assert client.get("/public/resources").status_code == 401
    assert put(client, payload).status_code == 200
    current = resources(client)
    node = current["nodes"][0]
    assert node["node"]["status"] == "ONLINE"
    assert node["node"]["telemetry_status"] == "CURRENT"
    assert node["system"]["cpu"]["utilization_percent"] == 40
    assert node["compute"]["reported_processes"] == 1
    assert node["compute"]["user_scope"] == "unknown"
    assert node["system_freshness"] == node["compute_freshness"] == "CURRENT"
    old = status(client)
    assert "system" not in old["nodes"][0]
    duplicate = put(client, payload)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    changed = json.loads(json.dumps(payload))
    changed["resources"]["system"]["cpu"]["utilization_percent"] = 50
    assert put(client, changed).status_code == 409
    with Session(client.app.state.engine) as db:
        for model in [AgentMessage, DomainEvent, OutboxEntry, AuditEntry, NodeResource]:
            assert db.scalar(select(func.count()).select_from(model)) == int(
                model in (AgentMessage, NodeResource)
            )


def test_new_gpu_failure_does_not_erase_system_or_get_hidden_by_older_heartbeat(client):
    assert put(client, resource_batch()).status_code == 200
    assert put(client, resource_batch(seq=3, gpu_quality="error")).status_code == 200
    # This heartbeat is observed later but has an older global source sequence.
    assert beat(client, heartbeat(seq=2, collector_status="ok")).status_code == 200
    node = resources(client)["nodes"][0]
    assert node["node"]["collector_status"] == "error"
    assert node["node"]["telemetry_status"] == "ERROR"
    assert node["node"]["gpus"][0]["observed_state"] == "unknown"
    assert node["system"]["memory"]["quality"] == "ok"
    assert put(client, resource_batch(seq=4)).status_code == 200
    assert status(client)["nodes"][0]["telemetry_status"] == "CURRENT"


def test_component_freshness_and_missing_metrics_never_return_fake_zero(client):
    assert beat(client, heartbeat()).status_code == 200
    missing = resources(client)["nodes"][0]
    assert missing["system_freshness"] == "MISSING" and missing["system"] is None
    assert (
        put(client, resource_batch(seq=2, cpu_quality="warming", compute_age=120)).status_code
        == 200
    )
    node = resources(client)["nodes"][0]
    assert node["system"]["cpu"]["utilization_percent"] is None
    assert node["system"]["memory"]["available_bytes"] == 4096
    assert node["compute_freshness"] == "STALE" and node["compute"] is None
    assert node["compute_observed_at"] is not None


def test_stale_and_offline_component_values_are_removed(client):
    assert put(client, resource_batch(seconds_ago=190)).status_code == 200
    node = resources(client)["nodes"][0]
    assert node["node"]["status"] == "OFFLINE"
    assert node["system_freshness"] == node["compute_freshness"] == "STALE"
    assert node["system"] is None and node["compute"] is None


def test_old_gpu_component_cannot_replace_fresh_gpu_while_system_advances(client):
    assert put(client, resource_batch()).status_code == 200
    gpu_time = status(client)["nodes"][0]["observed_at"]
    payload = resource_batch(seq=2, utilization=70, gpu_quality="error")
    payload["resources"]["gpu"]["observed_at"] = (
        datetime.now(UTC) - timedelta(seconds=120)
    ).isoformat()
    assert put(client, payload).status_code == 200
    node = resources(client)["nodes"][0]
    assert node["node"]["observed_at"] == gpu_time
    assert node["node"]["telemetry_status"] == "CURRENT"
    assert node["system"]["cpu"]["utilization_percent"] == 70


def test_resource_clock_rollback_advances_fence_without_replacing_current_values(client):
    assert put(client, resource_batch(seq=10)).status_code == 200
    rollback = put(client, resource_batch(seq=30, seconds_ago=90, utilization=90))
    assert not rollback.json()["applied_to_current"]
    delayed = put(client, resource_batch(seq=20, utilization=80))
    assert not delayed.json()["applied_to_current"]
    node = resources(client)["nodes"][0]
    assert node["resource_seq"] == 10
    assert node["system"]["cpu"]["utilization_percent"] == 40
    assert put(client, resource_batch(seq=31, utilization=50)).status_code == 200
    assert resources(client)["nodes"][0]["system"]["cpu"]["utilization_percent"] == 50


def test_new_gpu_does_not_replace_newer_system_or_compute_components(client):
    assert put(client, resource_batch()).status_code == 200
    before = resources(client)["nodes"][0]
    payload = resource_batch(seq=2, utilization=90, compute_age=120)
    payload["resources"]["system"]["observed_at"] = (
        datetime.now(UTC) - timedelta(seconds=120)
    ).isoformat()
    assert put(client, payload).status_code == 200
    after = resources(client)["nodes"][0]
    assert after["node"]["observed_at"] > before["node"]["observed_at"]
    assert after["system"] == before["system"]
    assert after["compute"] == before["compute"]


def test_resource_projection_and_message_transaction_roll_back_together(client, monkeypatch):
    def failed(*args):
        raise RuntimeError("private failure")

    monkeypatch.setattr("aris.modules.vega.service.VegaService._apply_gpus", failed)
    result = put(client, resource_batch())
    assert result.status_code == 500 and "private" not in result.text
    with Session(client.app.state.engine) as db:
        for model in [Node, NodeResource, AgentMessage, DomainEvent, OutboxEntry, AuditEntry]:
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_historical_message_bytes_and_fingerprint_survive_migration_without_legacy_ingress(
    settings,
):
    command.downgrade(Config("alembic.ini"), "0003")
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from aris.apps.api.main import create_app
    from aris.core.db import build_engine

    payload = batch()
    payload.pop("type")
    payload.pop("resources")
    payload.update(schema_version="aris.agent/v1", gpus=[gpu.model_dump() for gpu in samples()])
    legacy = SimpleNamespace(**payload)
    legacy.observed_at = datetime.fromisoformat(legacy.observed_at)
    legacy.sent_at = datetime.fromisoformat(legacy.sent_at)
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    engine = build_engine(settings.database_url)
    with engine.begin() as db:
        db.execute(
            Node.__table__.insert().values(
                id=legacy.node_id,
                agent_id=legacy.agent_id,
                hostname=legacy.hostname,
                agent_version=legacy.agent_version,
                last_seq=legacy.seq,
                observed_at=legacy.observed_at,
                received_at=legacy.sent_at,
                telemetry_at=legacy.observed_at,
                telemetry_received_at=legacy.sent_at,
                telemetry_seq=legacy.seq,
            )
        )
        db.execute(
            AgentMessage.__table__.insert().values(
                id=str(legacy.message_id),
                message_id=str(legacy.message_id),
                agent_id=legacy.agent_id,
                node_id=legacy.node_id,
                seq=legacy.seq,
                message_kind="telemetry",
                fingerprint=fingerprint,
                observed_at=legacy.observed_at,
                received_at=legacy.sent_at,
                applied_to_current=True,
                payload=payload,
            )
        )
    command.upgrade(Config("alembic.ini"), "head")
    with TestClient(create_app(settings)) as client:
        result = post(client, payload)
        assert result.status_code == 422
    with engine.connect() as db:
        row = db.execute(select(AgentMessage.payload, AgentMessage.fingerprint)).one()
        assert row.payload == payload and row.fingerprint == fingerprint
    engine.dispose()


def test_non_utc_timestamp_is_stored_as_utc_without_changing_payload(client):
    instant = datetime.now(UTC) - timedelta(seconds=5)
    offset = instant.astimezone(timezone(timedelta(hours=8)))
    payload = batch(observed_at=offset.isoformat(), sent_at=offset.isoformat())
    assert post(client, payload).status_code == 200
    node = status(client)["nodes"][0]
    assert datetime.fromisoformat(node["observed_at"]) == instant
    assert node["telemetry_status"] == "CURRENT"
    assert post(client, payload).json()["duplicate"]


def test_0004_backfills_shared_health_without_resetting_existing_heartbeat(settings):
    from fastapi.testclient import TestClient

    from aris.apps.api.main import create_app
    from aris.core.db import build_engine
    from aris.core.time import utc

    command.downgrade(Config("alembic.ini"), "0003")
    engine = build_engine(settings.database_url)
    now = datetime.now(UTC)
    with engine.begin() as db:
        db.execute(
            Node.__table__.insert().values(
                id="node-a",
                agent_id="agent-a",
                hostname="host-a",
                agent_version="legacy",
                last_seq=7,
                observed_at=now,
                received_at=now,
                telemetry_at=now - timedelta(seconds=15),
                telemetry_seq=6,
                heartbeat_at=now,
                heartbeat_received_at=now,
                heartbeat_seq=7,
                collector_status="error",
            )
        )
    command.upgrade(Config("alembic.ini"), "head")
    with Session(engine) as db:
        node = db.get(Node, "node-a")
        assert node.collector_health_seq == node.heartbeat_seq == 7
        assert utc(node.collector_health_at) == now
    with TestClient(create_app(settings)) as client:
        assert status(client)["nodes"][0]["telemetry_status"] == "ERROR"
    engine.dispose()


def test_spool_replays_exact_resources_and_heartbeats_to_their_endpoints(client, tmp_path):
    spool = Spool(tmp_path / "mixed.sqlite3", "agent-a", "node-a")
    first = spool.enqueue_resource("host", resource_observation())
    second = spool.enqueue_resource("host", resource_observation())
    third = spool.enqueue_heartbeat("host", "ok")
    restarted = Spool(spool.path, "agent-a", "node-a")
    assert restarted.pending() == [first, second, third]
    paths = []

    def send(request):
        paths.append(request.url.path)
        response = client.post(
            request.url.path, content=request.content, headers={"X-Agent-Token": AGENT_TOKEN}
        )
        return httpx.Response(response.status_code, json=response.json())

    config = AgentSettings(_env_file=None, id="agent-a", node_id="node-a", token=AGENT_TOKEN)
    with httpx.Client(transport=httpx.MockTransport(send)) as transport:
        assert flush(restarted, config, transport) == 3
    assert set(paths) == {
        "/vega/heartbeat",
        "/vega/telemetry/batch",
    }
    assert restarted.pending() == []
    assert resources(client)["nodes"][0]["resource_seq"] == second.seq


def test_postgres_resource_first_reports_share_node_lock_and_dedupe(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL resource row locking")
    payload = resource_batch()
    start = Barrier(2)

    def send(_):
        start.wait(timeout=5)
        response = put(client, payload)
        return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert [code for code, _ in results] == [200, 200]
    assert sorted(item["duplicate"] for _, item in results) == [False, True]
    with Session(client.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(NodeResource)) == 1
        assert db.scalar(select(func.count()).select_from(AgentMessage)) == 1
