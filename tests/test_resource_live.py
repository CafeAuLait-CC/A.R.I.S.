import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.live import PreviewSession, live_url
from aris.apps.agent.main import CollectionFailed, tick
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.spool import Spool
from aris.apps.api.main import create_app
from aris.apps.dashboard.main import preview_proxy, resource_events
from aris.core.db import session_factory
from aris.modules.vega.live import LiveHub
from aris.modules.vega.live_schemas import PreviewLease, ResourceLiveFrame, ResourceLiveHello
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.service import VegaService
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_live import frame, hello
from tests.test_resource_telemetry import resource_observation, resources


def resource_hello():
    return ResourceLiveHello(
        agent_id="agent-a", node_id="node-a", hostname="test-node", session_id=uuid4()
    )


@pytest.mark.parametrize("gpu_quality", ["ok", "error"])
def test_resource_tick_preserves_healthy_system_and_still_writes_only_two_messages(
    client, tmp_path, monkeypatch, gpu_quality
):
    sampler = ResourceSampler()
    observation = resource_observation(gpu_quality=gpu_quality)
    monkeypatch.setattr(sampler, "sample", lambda **_: observation)
    settings = AgentSettings(
        _env_file=None, id="agent-a", node_id="node-a", token=AGENT_TOKEN, resource_enabled=True
    )
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    requests = []

    def send(request):
        requests.append((request.url.path, json.loads(request.content)))
        response = client.post(
            request.url.path, headers={"X-Agent-Token": AGENT_TOKEN}, json=requests[-1][1]
        )
        return httpx.Response(response.status_code, json=response.json())

    with httpx.Client(transport=httpx.MockTransport(send)) as http:
        if gpu_quality == "error":
            with pytest.raises(CollectionFailed):
                tick(spool, settings, http, sampler=sampler)
        else:
            assert tick(spool, settings, http, sampler=sampler) == {"seq": 2, "delivered": 2}
    assert [path for path, _ in requests] == ["/vega/v1/heartbeat", "/vega/v2/telemetry/batch"]
    assert requests[0][1]["capabilities"] == ["node.resources/v2", "agent.heartbeat/v1"]
    assert requests[0][1]["collector_status"] == ("ok" if gpu_quality == "ok" else "error")
    assert spool.pending() == []
    node = resources(client)["nodes"][0]
    assert node["system"]["memory"]["available_bytes"] == 4096
    with Session(client.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(AgentMessage)) == 2


def test_resource_preview_uses_read_only_lease_and_shares_archival_sample(tmp_path, monkeypatch):
    now = [100.0]
    calls = []
    sampler = ResourceSampler()

    def sample(**kwargs):
        observation = resource_observation()
        calls.append(observation)
        return observation

    monkeypatch.setattr(sampler, "sample", sample)
    preview = PreviewSession(sampler, uuid4(), clock=lambda: now[0])
    assert preview.frame() is None and not calls
    preview.apply(PreviewLease(lease_seconds=3))
    first = preview.frame()
    assert isinstance(first, ResourceLiveFrame)
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    archived = spool.enqueue_resource("test-node", calls[0])
    assert archived.resources == first.resources and archived.seq == 1
    now[0] += 1
    assert preview.frame().observed_at > first.observed_at
    assert len(spool.pending()) == 1
    preview.apply(PreviewLease(lease_seconds=0))
    now[0] += 1
    assert preview.frame() is None and len(calls) == 2


def test_slow_resource_sampling_cannot_extend_its_own_lease(monkeypatch):
    now = [1.0]
    sampler = ResourceSampler()

    def slow(**kwargs):
        now[0] += 5
        return resource_observation()

    monkeypatch.setattr(sampler, "sample", slow)
    preview = PreviewSession(sampler, uuid4(), clock=lambda: now[0])
    preview.apply(PreviewLease(lease_seconds=2))
    assert preview.frame() is None


def test_resource_hub_keeps_component_quality_independent_and_clears_disconnected_data():
    hub = LiveHub()
    connection = hub.attach(resource_hello())
    key, _ = hub.open_viewer()
    observation = resource_observation(gpu_quality="error", compute_age=16)
    hub.accept(
        connection,
        ResourceLiveFrame(session_id=connection.hello.session_id, seq=1, resources=observation),
    )
    node = hub.resource_snapshot().nodes[0]
    assert node.quality == "error" and node.gpus == []
    assert node.system_freshness == node.compute_freshness == "CURRENT"
    assert node.system.memory.available_bytes == 4096
    assert node.compute.observed_at == observation.compute.observed_at
    assert "system" not in hub.snapshot().model_dump()["nodes"][0]
    hub.detach(connection)
    lost = hub.resource_snapshot().nodes[0]
    assert lost.system is None and lost.compute is None and lost.gpus == []
    assert lost.system_freshness == lost.compute_freshness == "STALE"
    hub.close_viewer(key)


def test_resource_hub_expires_components_by_their_own_clock_and_transport_age():
    now = [1.0]
    hub = LiveHub(clock=lambda: now[0])
    connection = hub.attach(resource_hello())
    hub.open_viewer()
    observation = resource_observation(compute_age=31)
    observation.system.observed_at = datetime.now(UTC) - timedelta(seconds=6)
    hub.accept(
        connection,
        ResourceLiveFrame(session_id=connection.hello.session_id, seq=1, resources=observation),
    )
    node = hub.resource_snapshot().nodes[0]
    assert node.quality == "live" and node.gpus
    assert node.system is None and node.compute is None
    assert node.system_observed_at == observation.system.observed_at
    now[0] += 4
    assert hub.resource_snapshot().nodes[0].gpus == []


def test_old_collector_has_missing_resources_and_mixed_frame_versions_are_rejected():
    hub = LiveHub()
    connection = hub.attach(hello())
    hub.open_viewer()
    hub.accept(connection, frame(connection))
    node = hub.resource_snapshot().nodes[0]
    assert node.system_freshness == node.compute_freshness == "MISSING"
    assert node.system is None and node.compute is None
    with pytest.raises(ValueError, match="session"):
        hub.accept(
            connection,
            ResourceLiveFrame(
                session_id=connection.hello.session_id, seq=2, resources=resource_observation()
            ),
        )


def test_v2_websocket_is_authenticated_identity_bound_and_does_no_sql(settings, monkeypatch):
    app = create_app(settings)
    queries = []
    received = threading.Event()
    event.listen(app.state.engine, "before_cursor_execute", lambda *args: queries.append(True))
    hub = app.state.live_hub
    original = hub.accept

    def accept(connection, sample):
        original(connection, sample)
        received.set()

    monkeypatch.setattr(hub, "accept", accept)
    with TestClient(app) as client:
        assert client.get("/internal/v2/live").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/vega/v2/live?agent_id=agent-a&node_id=node-a"):
                pass
        subscription = hub.subscribe()
        client.portal.call(subscription.__aenter__)
        try:
            with client.websocket_connect(
                "/vega/v2/live?agent_id=agent-a&node_id=node-a",
                headers={"X-Agent-Token": AGENT_TOKEN},
            ) as ws:
                greeting = resource_hello()
                ws.send_text(greeting.model_dump_json())
                assert ws.receive_json()["lease_seconds"] == 30
                ws.send_text(
                    ResourceLiveFrame(
                        session_id=greeting.session_id, seq=1, resources=resource_observation()
                    ).model_dump_json()
                )
                assert received.wait(timeout=2)
                snapshot = client.portal.call(hub.resource_snapshot)
                assert snapshot.nodes[0].system.cpu.utilization_percent == 40
                assert AGENT_TOKEN not in snapshot.model_dump_json()
        finally:
            client.portal.call(subscription.__aexit__, None, None, None)
    assert queries == []
    config = AgentSettings(
        _env_file=None,
        id="agent-a",
        node_id="node-a",
        token=AGENT_TOKEN,
        api_url="https://control.example",
        resource_enabled=True,
    )
    assert "/vega/v2/live?" in live_url(config)


def test_resource_dashboard_archive_stream_retains_component_freshness(client, settings):
    from tests.test_resource_telemetry import put, resource_batch

    assert put(client, resource_batch(compute_age=120)).status_code == 200
    service = VegaService(session_factory(client.app.state.engine), settings)

    async def connected():
        return False

    async def consume():
        stream = resource_events(service, connected, resource_mode=True)
        await anext(stream)
        message = await anext(stream)
        snapshot = json.loads(message.split("data: ")[1])
        node = snapshot["nodes"][0]
        assert snapshot["schema_version"] == "aris.resources/v2"
        assert node["system"]["cpu"]["utilization_percent"] == 40
        assert node["compute"] is None and node["compute_freshness"] == "STALE"
        assert READ_TOKEN not in message and AGENT_TOKEN not in message
        await stream.aclose()

    asyncio.run(consume())


def test_resource_preview_proxy_uses_v2_and_keeps_credentials_off_browser(settings, monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def send(request):
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                'event: preview\ndata: {"schema_version":"aris.live-preview/v2",'
                '"generated_at":"2026-01-01T00:00:00Z","nodes":[]}\n\n'
            ),
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(send)),
    )

    async def connected():
        return False

    async def consume():
        stream = preview_proxy(settings, "http://127.0.0.1:8000", connected, resource_mode=True)
        await anext(stream)
        message = await anext(stream)
        assert "aris.live-preview/v2" in message and READ_TOKEN not in message
        await stream.aclose()

    asyncio.run(consume())
    assert requests[0].url.path == "/internal/v2/live"
    assert requests[0].headers["Authorization"] == "Bearer " + READ_TOKEN
