"""Keep only the actually deployed db48890 Collector compatible, with frozen wire bytes."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.modules.vega.live_schemas import LiveFrame, LiveHello
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_telemetry import batch, post

BASELINE = json.loads((Path(__file__).parent / "fixtures/vega-db48890.json").read_text())


@pytest.mark.parametrize(
    "kind,model",
    [
        ("telemetry", ResourceTelemetryBatch),
        ("heartbeat", Heartbeat),
        ("hello", LiveHello),
        ("frame", LiveFrame),
    ],
)
def test_exact_deployed_model_wire_and_fingerprint_are_unchanged(kind, model):
    assert BASELINE["source_commit"] == "db488904835bef68b884f86f99d13af2a5e3b035"
    parsed = model.model_validate(BASELINE[kind])
    payload = parsed.model_dump(mode="json")
    assert payload == BASELINE[kind]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == BASELINE[kind + "_sha256"]
    assert json.loads(parsed.model_dump_json()) == BASELINE[kind]


def test_deployed_retries_and_new_ownership_share_one_protocol_without_rewriting_history(client):
    assert post(client, BASELINE["telemetry"]).status_code == 200
    headers = {"X-Agent-Token": AGENT_TOKEN}
    assert (
        client.post("/vega/heartbeat", json=BASELINE["heartbeat"], headers=headers).status_code
        == 200
    )
    current = batch(seq=3)
    current["resources"]["compute"].update(
        user_scope="reported_linux_users",
        ownership={
            "quality": "ok",
            "unresolved_processes": 0,
            "users": [
                {
                    "uid": 1000,
                    "username": "fixture-user",
                    "reported_processes": 1,
                    "gpus": [
                        {"gpu_uuid": "GPU-a", "reported_processes": 1, "reported_memory_mib": None}
                    ],
                }
            ],
        },
    )
    assert post(client, current).status_code == 200
    assert post(client, BASELINE["telemetry"]).json()["duplicate"] is True
    assert (
        client.post("/vega/heartbeat", json=BASELINE["heartbeat"], headers=headers).json()[
            "duplicate"
        ]
        is True
    )
    with Session(client.app.state.engine) as session:
        for kind in ("telemetry", "heartbeat"):
            stored = session.scalars(
                select(AgentMessage).where(AgentMessage.message_id == BASELINE[kind]["message_id"])
            ).one()
            assert stored.payload == BASELINE[kind]
            assert stored.fingerprint == BASELINE[kind + "_sha256"]
    status = client.get("/public/resources", headers={"Authorization": "Bearer " + READ_TOKEN})
    node = status.json()["nodes"][0]
    assert node["resource_seq"] == 3
    assert node["compute"]["ownership"]["users"][0]["uid"] == 1000
    with client.websocket_connect(
        "/vega/live?agent_id=agent-a&node_id=node-a", headers=headers
    ) as socket:
        socket.send_json(BASELINE["hello"])
        assert socket.receive_json()["lease_seconds"] == 0
