import hashlib
import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.modules.vega.models import AgentMessage, NodeResource
from tests.conftest import AGENT_TOKEN
from tests.test_resource_telemetry import put, resource_batch, resources


@pytest.mark.parametrize("prefix", ["v1", "v2"])
def test_removed_development_paths_have_no_aliases(client, prefix):
    assert client.post(f"/vega/{prefix}/telemetry/batch", json={}).status_code == 404
    assert client.post(f"/vega/{prefix}/heartbeat", json={}).status_code == 404
    assert client.get(f"/public/{prefix}/resources").status_code == 404
    assert client.get(f"/internal/{prefix}/live").status_code == 404


def test_gpu_only_and_previous_resource_envelopes_are_not_ingress_formats(client):
    current = resource_batch()
    assert put(client, current).status_code == 200
    for marker in ("aris.agent/v1", "aris.telemetry/v2"):
        old = {**resource_batch(seq=2), "schema_version": marker}
        old.pop("type")
        if marker == "aris.agent/v1":
            old["gpus"] = old.pop("resources")["gpu"]["gpus"]
        assert put(client, old).status_code == 422
    heartbeat = {**current, "type": "heartbeat"}
    assert (
        client.post(
            "/vega/heartbeat", json=heartbeat, headers={"X-Agent-Token": AGENT_TOKEN}
        ).status_code
        == 422
    )


def test_projection_cleanup_keeps_original_history_fingerprints_and_current_facts(client):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    config = Config("alembic.ini")
    command.downgrade(config, "0004")
    engine = client.app.state.engine
    with Session(engine) as db:
        row = db.scalars(select(AgentMessage)).one()
        # Simulate a message already archived by the previous development Collector.
        archived = {**row.payload, "schema_version": "aris.telemetry/v2"}
        archived.pop("type")
        archived["resources"] = {
            **archived["resources"],
            "schema_version": "aris.resource-observation/v1",
        }
        row.payload = archived
        row.fingerprint = hashlib.sha256(
            json.dumps(archived, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        db.commit()
        before = (row.message_id, row.seq, row.fingerprint, row.payload)
        projection = db.scalars(select(NodeResource)).one()
        prior_projection = projection.observation.copy()
        seq = projection.seq
    command.upgrade(config, "head")
    command.check(config)
    with Session(engine) as db:
        row = db.scalars(select(AgentMessage)).one()
        assert (row.message_id, row.seq, row.fingerprint, row.payload) == before
        projection = db.scalars(select(NodeResource)).one()
        assert projection.seq == seq
        expected = {k: v for k, v in prior_projection.items() if k != "schema_version"}
        assert projection.observation == expected
    assert client.get("/health/ready").json()["schema_revision"] == "0016"
    assert resources(client)["nodes"][0]["system_freshness"] == "CURRENT"
