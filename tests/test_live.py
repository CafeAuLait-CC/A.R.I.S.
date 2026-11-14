import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event
from starlette.websockets import WebSocketDisconnect

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.live import PreviewSession, live_url, outbound_connection
from aris.apps.agent.sampler import Sampler
from aris.apps.agent.spool import Spool
from aris.apps.api.main import create_app
from aris.modules.vega.live import LiveHub
from aris.modules.vega.live_schemas import LiveFrame, LiveHello, PreviewLease
from tests.conftest import AGENT_TOKEN
from tests.test_agent import samples


def hello(**kwargs):
    return LiveHello(
        agent_id="agent-a", node_id="node-a", hostname="test-node", session_id=uuid4(), **kwargs
    )


def frame(connection, seq=1, **kwargs):
    return LiveFrame(
        session_id=connection.hello.session_id,
        seq=seq,
        observed_at=datetime.now(UTC),
        quality="ok",
        gpus=samples(),
        **kwargs,
    )


def test_preview_only_samples_during_a_bounded_read_only_lease():
    now = [100.0]
    calls = []
    sampler = Sampler(lambda: calls.append(True) or samples(), clock=lambda: now[0])
    session = PreviewSession(sampler, uuid4(), clock=lambda: now[0])
    assert session.frame() is None
    assert calls == []
    session.apply(PreviewLease(lease_seconds=3))
    first = session.frame()
    assert first.quality == "ok" and first.seq == 1
    assert session.frame() is None
    now[0] += 1
    assert session.frame().seq == 2
    now[0] += 3
    assert session.frame() is None
    assert len(calls) == 2  # A lost stop message cannot keep the sampler running.
    session.apply(PreviewLease(lease_seconds=30))
    assert session.frame().seq == 3
    session.apply(PreviewLease(lease_seconds=0))
    now[0] += 1
    assert session.frame() is None
    with pytest.raises(ValidationError):
        PreviewLease.model_validate({"lease_seconds": 999, "command": "shell"})


def test_live_and_archive_share_observation_time_without_changing_durable_sequence(tmp_path):
    calls = []
    sampler = Sampler(lambda: calls.append(True) or samples())
    live = PreviewSession(sampler, uuid4())
    live.apply(PreviewLease(lease_seconds=30))
    preview = live.frame()
    archive = sampler.sample()
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    message = spool.enqueue("test-node", archive.gpus, archive.observed_at)
    assert len(calls) == 1
    assert message.observed_at == preview.observed_at
    assert message.seq == 1
    assert len(spool.pending()) == 1  # Live frames never entered this spool.


def test_slow_driver_never_turns_a_cached_observation_into_a_new_preview_frame():
    now = [100.0]
    calls = []

    def slow_driver():
        calls.append(now[0])
        now[0] += 0.4
        return samples()

    sampler = Sampler(slow_driver, clock=lambda: now[0])
    preview = PreviewSession(sampler, uuid4(), clock=lambda: now[0])
    preview.apply(PreviewLease(lease_seconds=30))
    first = preview.frame()
    now[0] = 101.0  # Next preview is due, but completion-based cache is only 0.6s old.
    second = preview.frame()
    assert second.observed_at != first.observed_at
    assert len(calls) == 2
    now[0] = 103.0
    archive = sampler.sample()
    third = preview.frame()
    assert third.observed_at == archive.observed_at
    assert len(calls) == 3  # A newer archival read can still be shared.


def test_failed_or_slow_collection_never_replays_current_metrics_past_lease():
    now = [10.0]

    def slow():
        now[0] += 5
        return samples()

    slow_session = PreviewSession(Sampler(slow), uuid4(), clock=lambda: now[0])
    slow_session.apply(PreviewLease(lease_seconds=2))
    assert slow_session.frame() is None

    def broken():
        raise RuntimeError("private driver output")

    failed = PreviewSession(Sampler(broken), uuid4())
    failed.apply(PreviewLease(lease_seconds=30))
    failed_frame = failed.frame()
    assert failed_frame.quality == "error" and failed_frame.gpus == []
    assert "private" not in failed_frame.model_dump_json()


def test_hub_fences_reconnections_and_bounds_slow_viewers_without_persistence():
    async def check():
        now = [10.0]
        hub = LiveHub(clock=lambda: now[0], max_viewers=1)
        connection = hub.attach(hello())
        assert hub.lease(connection).lease_seconds == 0
        async with hub.subscribe() as (key, viewer):
            assert hub.lease(connection).lease_seconds == 30
            with pytest.raises(Exception, match="Too many"):
                async with hub.subscribe():
                    pass
            for seq in range(1, 10):
                now[0] += 1
                hub.accept(connection, frame(connection, seq))
            assert viewer.changed.qsize() == 1
            assert hub.snapshot().nodes[0].seq == 9
            assert hub.snapshot().nodes[0].quality == "live"
            with pytest.raises(ValueError, match="sequence"):
                hub.accept(connection, frame(connection, 9))
            now[0] += 4
            assert hub.snapshot().nodes[0].quality == "stale"
            assert hub.snapshot().nodes[0].gpus == []
            replacement = hub.attach(hello())
            hub.detach(connection)
            with pytest.raises(ValueError):
                hub.accept(connection, frame(connection, 10))
            assert hub.collectors["node-a"] is replacement
            hub.renew(key, viewer)
            now[0] += 26
            assert hub.lease(replacement).lease_seconds == 0
        assert not hub.active()

    asyncio.run(check())


def test_error_frames_and_disconnect_clear_live_metrics():
    async def check():
        hub = LiveHub()
        connection = hub.attach(hello())
        async with hub.subscribe():
            hub.accept(
                connection,
                LiveFrame(
                    session_id=connection.hello.session_id,
                    seq=1,
                    observed_at=datetime.now(UTC),
                    quality="error",
                ),
            )
            assert hub.snapshot().nodes[0].quality == "error"
            hub.detach(connection)
            node = hub.snapshot().nodes[0]
            assert node.quality == "offline" and node.gpus == []

    asyncio.run(check())


def test_expired_viewer_cannot_reenter_and_exceed_capacity():
    now = [1.0]
    hub = LiveHub(clock=lambda: now[0], max_viewers=1)
    key, old = hub.open_viewer()
    now[0] += 26
    new_key, new = hub.open_viewer()
    assert not hub.renew(key, old)
    assert list(hub.viewers) == [new_key]
    assert hub.renew(new_key, new)


def test_preview_keeps_running_for_other_viewers_then_stops_for_the_last_one():
    hub = LiveHub()
    connection = hub.attach(hello())
    first, _ = hub.open_viewer()
    second, _ = hub.open_viewer()
    hub.close_viewer(first)
    assert hub.lease(connection).lease_seconds == 30
    hub.close_viewer(second)
    assert hub.lease(connection).lease_seconds == 0


def test_websocket_never_forwards_node_credentials_through_redirects():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []

    class Destination(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            received.append(("destination", self.headers.get("X-Agent-Token")))
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(Destination):
        def do_GET(self):
            received.append(("configured", self.headers.get("X-Agent-Token")))
            self.send_response(302)
            self.send_header("Location", f"ws://127.0.0.1:{destination.server_port}/untrusted")
            self.send_header("Content-Length", "0")
            self.end_headers()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (origin, destination)
    ]
    for worker in workers:
        worker.start()
    config = AgentSettings(
        _env_file=None,
        id="agent-a",
        node_id="node-a",
        token=AGENT_TOKEN,
        api_url=f"http://127.0.0.1:{origin.server_port}",
    )
    try:
        with pytest.raises(Exception, match="redirect|302"):
            with outbound_connection(config):
                pytest.fail("redirect handshake must fail")
        assert received == [("configured", AGENT_TOKEN)]
    finally:
        for server in (origin, destination):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


def test_websocket_auth_identity_and_zero_database_queries(settings, monkeypatch):
    app = create_app(settings)
    queries = []
    accepted = threading.Event()
    event.listen(app.state.engine, "before_cursor_execute", lambda *args: queries.append(True))
    hub = app.state.live_hub
    original = hub.accept

    def record(connection, sample):
        original(connection, sample)
        accepted.set()

    monkeypatch.setattr(hub, "accept", record)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/vega/v1/live?agent_id=agent-a&node_id=node-a"):
                pass
        assert client.get("/internal/v1/live").status_code == 401
        subscription = hub.subscribe()
        client.portal.call(subscription.__aenter__)
        try:
            with client.websocket_connect(
                "/vega/v1/live?agent_id=agent-a&node_id=node-a",
                headers={"X-Agent-Token": AGENT_TOKEN},
            ) as websocket:
                greeting = hello()
                websocket.send_text(greeting.model_dump_json())
                assert websocket.receive_json()["lease_seconds"] == 30
                websocket.send_text(
                    LiveFrame(
                        session_id=greeting.session_id,
                        seq=1,
                        observed_at=datetime.now(UTC),
                        quality="ok",
                        gpus=samples(),
                    ).model_dump_json()
                )
                assert accepted.wait(timeout=2)
                snapshot = client.portal.call(hub.snapshot)
                assert snapshot.nodes[0].quality == "live"
                assert AGENT_TOKEN not in snapshot.model_dump_json()
        finally:
            client.portal.call(subscription.__aexit__, None, None, None)
    assert queries == []


def test_websocket_rejects_claimed_identity_and_arbitrary_commands(settings):
    with TestClient(create_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/vega/v1/live?agent_id=agent-a&node_id=node-b",
                headers={"X-Agent-Token": AGENT_TOKEN},
            ):
                pass
        with client.websocket_connect(
            "/vega/v1/live?agent_id=agent-a&node_id=node-a",
            headers={"X-Agent-Token": AGENT_TOKEN},
        ) as ws:
            ws.send_text(json.dumps({"type": "shell", "command": "nvidia-smi"}))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_old_observations_are_not_live_and_outbound_transport_has_no_public_listener():
    async def check():
        hub = LiveHub()
        connection = hub.attach(hello())
        async with hub.subscribe():
            old = frame(connection).model_copy(
                update={"observed_at": datetime.now(UTC) - timedelta(seconds=30)}
            )
            hub.accept(connection, old)
            assert hub.snapshot().nodes[0].quality == "stale"
            assert hub.snapshot().nodes[0].gpus == []

    asyncio.run(check())
    config = AgentSettings(
        _env_file=None,
        id="agent-a",
        node_id="node-a",
        token=AGENT_TOKEN,
        api_url="https://control.example",
    )
    assert live_url(config) == "wss://control.example/vega/v1/live?agent_id=agent-a&node_id=node-a"
    assert not config.live_enabled
