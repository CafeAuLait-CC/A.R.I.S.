from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from aris.apps.api.main import create_app
from aris.modules.vega.live import LiveHub
from aris.modules.vega.live_schemas import LiveFrame, ResourceLiveHello
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_live import frame, hello

STATUS = "/internal/v1/live/status"


def test_relay_diagnostics_observe_lifecycle_without_granting_or_renewing_a_lease():
    now = [100.0]
    hub = LiveHub(clock=lambda: now[0])
    connection = hub.attach(hello())
    empty = hub.relay_status()
    assert empty.active_viewers == 0 and empty.connected_collectors == 1
    assert empty.nodes[0].state == "idle" and not empty.nodes[0].preview_requested
    assert empty.nodes[0].last_frame_seq is None
    assert empty.nodes[0].last_frame_age_seconds is None
    assert empty.nodes[0].last_input_age_seconds is None
    key, viewer = hub.open_viewer()
    expiry, controls = viewer.expires, connection.control.qsize()
    assert hub.relay_status().nodes[0].state == "waiting"
    assert viewer.expires == expiry and connection.control.qsize() == controls
    hub.accept(connection, frame(connection))
    live = hub.relay_status().nodes[0]
    assert live.state == "live" and live.preview_requested
    assert live.received_frames == live.accepted_frames == 1
    assert live.ignored_inactive_frames == 0 and live.last_frame_seq == 1
    now[0] += 4
    assert hub.relay_status().nodes[0].state == "stale"
    hub.accept(
        connection,
        LiveFrame(
            session_id=connection.hello.session_id,
            seq=2,
            observed_at=datetime.now(UTC),
            quality="error",
        ),
    )
    assert hub.relay_status().nodes[0].state == "error"
    now[0] -= 1
    regressed = hub.relay_status().nodes[0]
    assert regressed.state == "stale"
    assert regressed.last_frame_age_seconds is regressed.last_input_age_seconds is None
    now[0] += 1
    hub.close_viewer(key)
    assert hub.relay_status().nodes[0].state == "idle"
    hub.accept(connection, frame(connection, seq=3))
    inactive = hub.relay_status().nodes[0]
    assert inactive.received_frames == 3 and inactive.accepted_frames == 2
    assert inactive.ignored_inactive_frames == 1
    assert inactive.last_frame_seq == 2 and inactive.last_input_seq == 3
    assert not inactive.preview_requested
    hub.detach(connection)
    offline = hub.relay_status()
    assert offline.connected_collectors == 0 and offline.nodes[0].state == "offline"
    replacement = hub.attach(hello())
    replaced = hub.relay_status().nodes[0]
    assert replaced.session_id == replacement.hello.session_id
    assert replaced.session_id != connection.hello.session_id
    assert replaced.received_frames == replaced.accepted_frames == 0
    assert replaced.last_input_seq is None


def test_status_excludes_expired_viewers_without_pruning_them_or_sending_control():
    now = [100.0]
    hub = LiveHub(clock=lambda: now[0], max_viewers=2)
    connection = hub.attach(hello())
    old, old_viewer = hub.open_viewer()
    now[0] = 120
    fresh, fresh_viewer = hub.open_viewer()
    before = dict(hub.viewers)
    controls = connection.control.qsize()
    now[0] = 126
    status = hub.relay_status()
    assert status.active_viewers == 1 and status.viewer_capacity == 2
    assert status.nodes[0].state == "waiting"
    now[0] = 151
    status = hub.relay_status()
    assert status.active_viewers == 0 and status.nodes[0].state == "idle"
    assert hub.viewers == before
    assert hub.viewers[old] is old_viewer and old_viewer.expires == 125
    assert hub.viewers[fresh] is fresh_viewer and fresh_viewer.expires == 145
    assert connection.control.qsize() == controls


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer invalid"},
        {"Authorization": "Bearer " + AGENT_TOKEN},
        {"X-Agent-Token": AGENT_TOKEN},
    ],
)
def test_relay_status_requires_platform_read_credentials(client, headers):
    response = client.get(STATUS, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert client.app.state.live_hub.viewers == {}


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_authenticated_relay_status_uses_no_sql_and_creates_no_viewer(settings, version):
    app = create_app(settings)
    statements = []

    @event.listens_for(app.state.engine, "before_cursor_execute")
    def no_database(*args):
        statements.append(args[2])
        raise AssertionError("relay diagnostics must not query PostgreSQL")

    identity = hello()
    if version == "v2":
        identity = ResourceLiveHello.model_validate(identity.model_dump(exclude={"schema_version"}))
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/vega/{version}/live?agent_id=agent-a&node_id=node-a",
            headers={"X-Agent-Token": AGENT_TOKEN},
        ) as socket:
            socket.send_json(identity.model_dump(mode="json"))
            assert socket.receive_json()["lease_seconds"] == 0
            for _ in range(2):
                response = client.get(STATUS, headers={"Authorization": "Bearer " + READ_TOKEN})
                assert response.status_code == 200
                data = response.json()
                assert data["schema_version"] == "aris.live-relay-status/v1"
                assert data["scope"] == "api_process"
                assert data["active_viewers"] == 0 and data["connected_collectors"] == 1
                node = data["nodes"][0]
                assert node["state"] == "idle" and node["protocol"] == f"aris.live/{version}"
                assert node["last_frame_seq"] is None and node["last_frame_age_seconds"] is None
                assert node["received_frames"] == node["accepted_frames"] == 0
                assert app.state.live_hub.viewers == {}
                assert AGENT_TOKEN not in response.text and READ_TOKEN not in response.text
                assert "gpus" not in node and "resources" not in node and "hostname" not in node
    assert statements == []
