"""Abruptly kill only fixture writers, never a running API or hardware Collector."""

import hashlib
import json
import selectors
import signal
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import contextmanager

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.lock import collector_lock
from aris.apps.agent.spool import Spool
from aris.apps.agent.spool_status import inspect_spool
from aris.apps.agent.transport import flush
from aris.modules.vega.models import AgentMessage
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.resource_fixtures import resource_observation


@contextmanager
def fixture_writer(path, mode):
    program = textwrap.dedent("""
        import json, sqlite3, sys
        from pathlib import Path
        from aris.apps.agent.lock import collector_lock
        from aris.apps.agent.spool import Spool

        path, mode = Path(sys.argv[1]), sys.argv[2]
        with collector_lock(path):
            if mode == 'before_commit':
                db = sqlite3.connect(path)
                db.execute('PRAGMA cache_size=1')
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE metadata SET value='2' WHERE key='last_seq'")
                db.execute("INSERT INTO pending VALUES (2,'uncommitted-row','heartbeat')")
                db.execute('INSERT INTO spill_fixture VALUES (zeroblob(200000))')
                print(json.dumps({'phase': 'uncommitted'}), flush=True)
            else:
                spool = Spool(path, 'agent-a', 'node-a')
                heartbeat = spool.enqueue_heartbeat('fixture-host', 'ok')
                message = heartbeat.model_dump(mode='json')
                print(json.dumps({'phase': 'committed', 'message': message}), flush=True)
            sys.stdin.read()
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(path), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "fixture writer did not reach its crash point"
        line = child.stdout.readline()
        assert line, "fixture writer exited before its crash point"
        yield child, json.loads(line)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_uncommitted_spill_is_not_repaired_by_diagnostic_then_writer_recovers_originals(tmp_path):
    path = tmp_path / "spool.sqlite3"
    spool = Spool(path, "agent-a", "node-a")
    original = spool.enqueue_resource("fixture-host", resource_observation())
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE spill_fixture (data BLOB)")
    inode = path.stat().st_ino
    with fixture_writer(path, "before_commit") as (child, marker):
        assert marker["phase"] == "uncommitted"
        journal = path.with_name(path.name + "-journal")
        assert journal.is_file() and journal.stat().st_size > 512
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
        before = {file: file.read_bytes() for file in tmp_path.iterdir()}
        status = inspect_spool(path, "agent-a", "node-a")
        assert status["status"] == "unknown" and status["reason"] == "recovery_required"
        assert status["statistics"] is None
        assert {file: file.read_bytes() for file in tmp_path.iterdir()} == before
    lock_inode = path.with_name(path.name + ".lock").stat().st_ino
    with collector_lock(path):
        recovered = Spool(path, "agent-a", "node-a")
        assert recovered.pending() == [original]
        assert recovered.statistics()["last_seq"] == 1
        assert recovered.enqueue_heartbeat("fixture-host", "ok").seq == 2
    assert path.stat().st_ino == inode
    assert path.with_name(path.name + ".lock").stat().st_ino == lock_inode
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM spill_fixture").fetchone()[0] == 0


def test_crash_after_remote_commit_before_ack_replays_exact_messages_and_keeps_current_facts(
    tmp_path, client
):
    path = tmp_path / "spool.sqlite3"
    spool = Spool(path, "agent-a", "node-a")
    original = spool.enqueue_resource("fixture-host", resource_observation())
    inode = path.stat().st_ino
    with fixture_writer(path, "after_commit") as (child, marker):
        assert marker["phase"] == "committed"
        heartbeat = marker["message"]
        # The server commits this exact message; the fixture writer never sees
        # the acknowledgement and is killed while retaining its original row.
        accepted = client.post(
            "/vega/heartbeat", json=heartbeat, headers={"X-Agent-Token": AGENT_TOKEN}
        )
        assert accepted.status_code == 200 and accepted.json()["duplicate"] is False
        with sqlite3.connect(path) as db:
            frozen = dict(db.execute("SELECT seq,payload FROM pending"))
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    lock_inode = path.with_name(path.name + ".lock").stat().st_ino
    receipts = []

    def deliver(request):
        response = client.post(
            request.url.path,
            json=json.loads(request.content),
            headers={"X-Agent-Token": AGENT_TOKEN},
        )
        assert response.status_code == 200
        receipts.append(response.json())
        return httpx.Response(200, json=response.json())

    with collector_lock(path):
        restarted = Spool(path, "agent-a", "node-a")
        with sqlite3.connect(path) as db:
            assert dict(db.execute("SELECT seq,payload FROM pending")) == frozen
        fresh = restarted.enqueue_resource("fixture-host", resource_observation())
        assert fresh.seq == 3
        expected = {item.seq: item.model_dump(mode="json") for item in restarted.pending()}
        config = AgentSettings(_env_file=None, id="agent-a", node_id="node-a", token=AGENT_TOKEN)
        with httpx.Client(transport=httpx.MockTransport(deliver)) as transport:
            assert flush(restarted, config, transport) == 3
        assert restarted.pending() == [] and restarted.statistics()["last_seq"] == 3
    assert receipts[0]["duplicate"] is True
    assert receipts[1]["message_id"] == str(fresh.message_id)
    assert receipts[2]["message_id"] == str(original.message_id)
    assert receipts[2]["applied_to_current"] is False
    with Session(client.app.state.engine) as db:
        stored = list(db.scalars(select(AgentMessage)))
        assert len(stored) == 3
        for item in stored:
            assert item.payload == expected[item.seq]
            canonical = json.dumps(item.payload, sort_keys=True, separators=(",", ":")).encode()
            assert item.fingerprint == hashlib.sha256(canonical).hexdigest()
    state = client.get("/public/resources", headers={"Authorization": "Bearer " + READ_TOKEN})
    assert state.json()["nodes"][0]["resource_seq"] == 3
    assert path.stat().st_ino == inode
    assert path.with_name(path.name + ".lock").stat().st_ino == lock_inode
