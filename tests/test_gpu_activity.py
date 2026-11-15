from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.modules.vega.gpu_activity import gpu_activity
from aris.modules.vega.live import LiveHub
from aris.modules.vega.live_schemas import LiveFrame, LiveHello
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.resource_schemas import ResourceObservation
from tests.test_telemetry import batch, post, status


@pytest.mark.parametrize(
    ("memory", "utilization", "processes", "expected"),
    [
        (64, 0, 0, "idle"),
        (65, 0, 0, "busy"),
        (0, 1, 0, "busy"),
        (0, 0, 1, "busy"),
        (9, 0, None, "unknown"),
        (0, 0, None, "unknown"),
    ],
)
def test_activity_threshold_and_unknown_process_visibility(
    memory, utilization, processes, expected
):
    assert gpu_activity(memory, utilization, processes) == expected


def preview_states(payload):
    hub = LiveHub()
    connection = hub.attach(
        LiveHello(agent_id="agent-a", node_id="node-a", hostname="host-a", session_id=uuid4())
    )
    key, _ = hub.open_viewer()
    try:
        hub.accept(
            connection,
            LiveFrame(
                session_id=connection.hello.session_id,
                seq=1,
                resources=ResourceObservation.model_validate(payload["resources"]),
            ),
        )
        return {gpu.gpu_uuid: gpu.observed_state for gpu in hub.snapshot().nodes[0].gpus}
    finally:
        hub.close_viewer(key)


def test_archive_and_preview_agree_on_driver_memory_and_sleeping_processes_by_uuid(client):
    first = batch()["resources"]["gpu"]["gpus"][0]
    values = [
        ("GPU-a", 1, 0),
        ("GPU-b", 9, 0),
        ("GPU-c", 0, 0),
        ("GPU-d", 4096, 0),
        ("GPU-e", 9, 1),
    ]
    payload = batch(
        gpus=[
            {
                **first,
                "gpu_uuid": uid,
                "index": index,
                "memory_used_mib": memory,
                "utilization_percent": utilization,
            }
            for index, (uid, memory, utilization) in enumerate(values)
        ]
    )
    payload["resources"]["compute"]["gpus"] = [
        {"gpu_uuid": uid, "reported_processes": int(uid == "GPU-c"), "reported_memory_mib": None}
        for uid, *_ in reversed(values)
    ]
    assert post(client, payload).status_code == 200
    with Session(client.app.state.engine) as session:
        original = session.scalars(select(AgentMessage)).one()
        frozen = (original.payload, original.fingerprint)
    expected = {"GPU-a": "idle", "GPU-b": "idle", "GPU-c": "busy", "GPU-d": "busy", "GPU-e": "busy"}
    assert {
        gpu["gpu_uuid"]: gpu["observed_state"] for gpu in status(client)["nodes"][0]["gpus"]
    } == expected
    assert preview_states(payload) == expected
    with Session(client.app.state.engine) as session:
        stored = session.scalars(select(AgentMessage)).one()
        assert (stored.payload, stored.fingerprint) == frozen
        assert session.scalar(select(func.count()).select_from(AgentMessage)) == 1
    assert all("observed_state" not in gpu for gpu in payload["resources"]["gpu"]["gpus"])


@pytest.mark.parametrize("visibility", ["stale", "error", "missing_gpu"])
def test_small_memory_does_not_become_idle_when_process_visibility_is_unknown(client, visibility):
    gpu = batch()["resources"]["gpu"]["gpus"][0]
    payload = batch(gpus=[{**gpu, "memory_used_mib": 9}])
    compute = payload["resources"]["compute"]
    compute["reported_processes"] = 0
    compute["gpus"][0]["reported_processes"] = 0
    if visibility == "stale":
        compute["observed_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    elif visibility == "error":
        compute.update(quality="error", reported_processes=None, gpus=[])
    else:
        compute["gpus"] = []
    assert post(client, payload).status_code == 200
    assert status(client)["nodes"][0]["gpus"][0]["observed_state"] == "unknown"
    assert preview_states(payload) == {"GPU-a": "unknown"}
