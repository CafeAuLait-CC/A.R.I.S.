import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.main import collect_tick
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.spool import Spool
from aris.apps.agent.transport import DeliveryWorker
from tests.conftest import AGENT_TOKEN
from tests.test_agent import samples
from tests.test_resource_telemetry import resource_observation


def settings(**kwargs):
    return AgentSettings(
        _env_file=None, id="agent-a", node_id="node-a", token=AGENT_TOKEN, **kwargs
    )


def sampler(monkeypatch):
    result = ResourceSampler()
    monkeypatch.setattr(result, "sample", lambda **_: resource_observation())
    return result


def test_blocked_http_keeps_sqlite_unlocked_for_sampling_and_health(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a", max_batches=10)
    source = sampler(monkeypatch)
    collect_tick(spool, sampler=source)
    entered, release = threading.Event(), threading.Event()

    def blocked(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(503)

    worker = DeliveryWorker(
        spool,
        settings(),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(blocked)),
        emit=lambda _: None,
    )
    worker.start()
    try:
        assert entered.wait(2)
        for _ in range(3):
            collect_tick(spool, sampler=source)
            worker.wake()
        assert not release.is_set()
        assert spool.statistics()["pending_batches"] == 4
        assert spool.statistics()["pending_heartbeats"] == 4
        assert spool.statistics()["last_seq"] == 8
    finally:
        release.set()
        worker.close()
    assert not worker.thread.is_alive()


def test_delivery_backoff_ignores_new_sample_wakes_and_logs_actual_acks(tmp_path, monkeypatch):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    source = sampler(monkeypatch)
    collect_tick(spool, sampler=source)
    now, requests, records = [0.0], [], []
    failed, complete, retried = threading.Event(), threading.Event(), threading.Event()

    def send(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(503)
        retried.set()
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message_id": payload["message_id"],
                "duplicate": False,
                "applied_to_current": True,
            },
        )

    def emit(record):
        records.append(record)
        if record.get("error_type"):
            failed.set()
        if spool.statistics()["pending_batches"] + spool.statistics()["pending_heartbeats"] == 0:
            complete.set()

    worker = DeliveryWorker(
        spool,
        settings(flush_max_messages=3),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(send)),
        emit=emit,
        backoff=lambda _: 10,
        clock=lambda: now[0],
    )
    worker.start()
    try:
        assert failed.wait(2)
        for _ in range(3):
            collect_tick(spool, sampler=source)
            worker.wake()
        assert not retried.wait(0.1)
        assert len(requests) == 1
        now[0] = 10
        worker.wake()
        assert complete.wait(2)
    finally:
        worker.close()
    assert [p["seq"] for p in requests[1:4]] == [8, 7, 1]
    assert records[0]["delivered"] == 0
    assert records[0]["http_status"] == 503
    assert AGENT_TOKEN not in json.dumps(records)
    acknowledged = [seq for record in records for seq in record.get("acknowledged_sequences", [])]
    assert sorted(acknowledged) == list(range(1, 9))
    assert spool.pending() == []


def test_heartbeat_capacity_is_independent_and_latest_facts_leave_room_for_history(tmp_path):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a", max_batches=2, max_heartbeats=3)
    spool.enqueue("node", samples())
    spool.enqueue("node", samples())
    for _ in range(3):
        spool.enqueue_heartbeat("node", "error")
    with sqlite3.connect(spool.path) as db:
        before = dict(db.execute("SELECT seq,payload FROM pending"))
    with pytest.raises(RuntimeError, match="unacknowledged data retained"):
        spool.enqueue("node", samples())
    with pytest.raises(RuntimeError, match="unacknowledged data retained"):
        spool.enqueue_heartbeat("node", "ok")
    with sqlite3.connect(spool.path) as db:
        assert dict(db.execute("SELECT seq,payload FROM pending")) == before
    assert [message.seq for message in spool.delivery_batch(3)] == [5, 2, 1]
    assert spool.statistics()["capacity_heartbeats"] == 3


def test_actual_daemon_samples_while_server_holds_delivery_and_stops_cleanly(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            release.wait(10)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "daemon.sqlite3"
    env = {
        **os.environ,
        "ARIS_AGENT_ID": "cadence-agent",
        "ARIS_AGENT_NODE_ID": "cadence-node",
        "ARIS_AGENT_TOKEN": AGENT_TOKEN,
        "ARIS_AGENT_API_URL": f"http://127.0.0.1:{server.server_port}",
        "ARIS_AGENT_SPOOL_PATH": str(path),
        "ARIS_AGENT_INTERVAL_SECONDS": "1",
        "ARIS_AGENT_RESOURCE_ENABLED": "true",
        "ARIS_AGENT_LIVE_ENABLED": "false",
        "ARIS_AGENT_MAX_BUFFERED_BATCHES": "20",
        "ARIS_AGENT_MAX_BUFFERED_HEARTBEATS": "20",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "aris.apps.agent.main", "--fixture", "examples/gpu-idle.json"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert entered.wait(5)
        deadline = time.monotonic() + 6
        while True:
            assert process.poll() is None
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                counts = dict(db.execute("SELECT kind,count(*) FROM pending GROUP BY kind"))
            if counts.get("telemetry", 0) >= 3 and counts.get("heartbeat", 0) >= 3:
                break
            if time.monotonic() >= deadline:
                pytest.fail("hardware sampling stalled behind HTTP delivery")
            time.sleep(0.05)
        assert not release.is_set()
    finally:
        release.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert process.returncode == 0
    stdout, _ = process.communicate(timeout=1)
    entries = [json.loads(line) for line in stdout.splitlines()]
    assert sum(entry["phase"] == "sample" for entry in entries) >= 3
    assert any(entry["phase"] == "delivery" for entry in entries)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] >= 6
