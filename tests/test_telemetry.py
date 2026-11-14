from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aris.apps.api.main import create_app
from aris.core.audit import AuditEntry
from aris.core.config import Settings
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.models import AgentMessage, Node
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.resource_fixtures import resource_observation


def batch(seq=1, seconds_ago=0, **changes):
    now = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    payload = {
        "type": "telemetry",
        "message_id": str(uuid4()),
        "agent_id": "agent-a",
        "node_id": "node-a",
        "hostname": "host-a",
        "agent_version": "test/1",
        "seq": seq,
        "observed_at": now.isoformat(),
        "sent_at": now.isoformat(),
        "gpus": [
            {
                "gpu_uuid": "GPU-a",
                "index": 0,
                "model": "RTX A6000",
                "memory_total_mib": 49140,
                "memory_used_mib": 0,
                "utilization_percent": 0,
                "temperature_c": 35,
            }
        ],
        **changes,
    }
    gpus = payload.pop("gpus")
    payload["resources"] = resource_observation(
        observed_at=datetime.fromisoformat(payload["observed_at"]).replace(tzinfo=UTC)
        if datetime.fromisoformat(payload["observed_at"]).tzinfo is None
        else datetime.fromisoformat(payload["observed_at"]),
        gpus=gpus,
    ).model_dump(mode="json")
    return payload


def post(client, payload, token=AGENT_TOKEN):
    return client.post(
        "/vega/telemetry/batch",
        json=payload,
        headers={"X-Agent-Token": token, "X-Request-ID": "contract-test"},
    )


def status(client):
    response = client.get("/public/resources", headers={"Authorization": f"Bearer {READ_TOKEN}"})
    assert response.status_code == 200, response.text
    data = response.json()
    return {**data, "nodes": [item["node"] for item in data["nodes"]]}


def test_idle_snapshot_is_persistent_and_audited(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    with client.app.state.engine.connect() as connection:
        expected_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert client.get("/health/ready").json()["schema_revision"] == expected_revision
    assert post(client, batch()).status_code == 200
    node = status(client)["nodes"][0]
    assert node["status"] == "ONLINE"
    assert node["gpus"][0]["observed_state"] == "idle"
    assert node["gpus"][0]["availability"] == "not_evaluated"
    with Session(client.app.state.engine) as session:
        for model in (AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1
        assert session.scalars(select(AuditEntry)).one().request_id == "contract-test"


def test_external_paths_use_vega_collector_without_redundant_api_prefix(client):
    assert set(client.get("/openapi.json").json()["paths"]) == {
        "/vega/telemetry/batch",
        "/vega/heartbeat",
        "/public/resources",
        "/health/live",
        "/health/ready",
        "/slack/commands",
        "/slack/events",
        "/internal/live",
        "/internal/live/status",
    }
    for old in ("/api/agent/v1/heartbeat", "/api/public/resources"):
        assert client.get(old).status_code == 404


def test_authentication_and_node_scope(client):
    assert post(client, batch(), "wrong").status_code == 401
    assert post(client, batch(node_id="node-b")).status_code == 403
    assert client.get("/public/resources").status_code == 401
    assert (
        client.get(
            "/public/resources", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}
        ).status_code
        == 401
    )
    assert status(client)["nodes"] == []


def test_duplicates_and_conflicting_reuse(client):
    payload = batch()
    assert post(client, payload).json()["duplicate"] is False
    assert post(client, payload).json()["duplicate"] is True
    assert post(client, {**payload, "hostname": "tampered"}).status_code == 409
    assert post(client, batch()).status_code == 409
    with Session(client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 1


def test_out_of_order_and_clock_rollback_do_not_overwrite_current(client):
    assert post(client, batch(seq=2, seconds_ago=1)).json()["applied_to_current"] is True
    assert (
        post(client, batch(seq=1, seconds_ago=120, gpus=[])).json()["applied_to_current"] is False
    )
    assert (
        post(client, batch(seq=3, seconds_ago=120, gpus=[])).json()["applied_to_current"] is False
    )
    node = status(client)["nodes"][0]
    assert node["gpus"][0]["present"] is True
    assert node["seq"] == 3  # sequence high watermark, independent of observation clock
    assert post(client, batch(seq=4)).json()["applied_to_current"] is True


@pytest.mark.parametrize("seconds_ago,expected", [(90, "STALE"), (300, "OFFLINE")])
def test_replaying_old_observations_does_not_revive_node(client, seconds_ago, expected):
    assert post(client, batch(seconds_ago=seconds_ago)).status_code == 200
    node = status(client)["nodes"][0]
    assert node["status"] == expected
    assert node["gpus"][0]["observed_state"] == "unknown"


def test_missing_gpu_becomes_unknown(client):
    assert post(client, batch()).status_code == 200
    assert post(client, batch(seq=2, gpus=[])).status_code == 200
    gpu = status(client)["nodes"][0]["gpus"][0]
    assert gpu["present"] is False
    assert gpu["observed_state"] == "unknown"


def test_node_cannot_claim_another_nodes_gpu_and_transaction_rolls_back(client):
    assert post(client, batch()).status_code == 200
    response = post(
        client, batch(agent_id="agent-b", node_id="node-b"), "second-agent-credential-12345"
    )
    assert response.status_code == 409
    with Session(client.app.state.engine) as session:
        assert session.get(Node, "node-b") is None
        for model in (AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_configuration_cannot_rebind_a_persisted_agent_to_another_node(client, settings):
    assert post(client, batch()).status_code == 200
    changed = Settings(
        _env_file=None,
        environment="test",
        database_url=settings.database_url,
        read_token=READ_TOKEN,
        agent_credentials={"agent-a": {"node_id": "node-b", "token": AGENT_TOKEN}},
    )
    with TestClient(create_app(changed), raise_server_exceptions=False) as changed_client:
        assert post(changed_client, batch(node_id="node-b")).status_code == 403
    assert [node["node_id"] for node in status(client)["nodes"]] == ["node-a"]
    with Session(client.app.state.engine) as session:
        assert session.get(Node, "node-b") is None
        for model in (AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "unknown"},
        {"seq": 0},
        {"observed_at": "2026-01-01T00:00:00"},
        {"unexpected": "secret-input"},
        {"sent_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    ],
)
def test_schema_and_future_clock_are_rejected_without_echoing_input(client, changes):
    response = post(client, batch(**changes))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["request_id"] == "contract-test"
    assert "secret-input" not in response.text
    assert status(client)["nodes"] == []


def test_fail_closed_without_credentials(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'empty.db'}",
            read_token="",
            agent_credentials={},
        )
    )
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert post(client, batch()).status_code == 401
        assert client.get("/public/resources").status_code == 401


def test_migration_roundtrip(client):
    # Same test runs in an isolated PostgreSQL schema in CI.
    command.check(Config("alembic.ini"))
    command.downgrade(Config("alembic.ini"), "base")
    assert client.get("/health/ready").status_code == 503
    command.upgrade(Config("alembic.ini"), "head")
    assert client.get("/health/ready").status_code == 200


def test_postgres_concurrent_duplicate_is_serialized(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locking; run the CI PostgreSQL job")
    payload = batch()
    start = Barrier(2)

    def concurrent_post(_):
        start.wait(timeout=10)
        return post(client, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(concurrent_post, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
    with Session(client.app.state.engine) as session:
        for model in (Node, AgentMessage, DomainEvent, OutboxEntry, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1
