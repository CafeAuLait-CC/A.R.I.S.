import json
import os
import selectors
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from aris.apps.agent.lock import CollectorAlreadyRunning, collector_lock
from aris.apps.agent.spool import Spool
from tests.conftest import AGENT_TOKEN
from tests.resource_fixtures import resource_observation


def test_conflicting_collector_exits_before_touching_original_spool_or_network(tmp_path):
    path = tmp_path / "same.sqlite3"
    spool = Spool(path, "original-agent", "original-node")
    spool.enqueue_resource("original-node", resource_observation())
    before, inode = path.read_bytes(), path.stat().st_ino
    env = {
        **os.environ,
        "ARIS_AGENT_ID": "different-agent",
        "ARIS_AGENT_NODE_ID": "different-node",
        "ARIS_AGENT_TOKEN": AGENT_TOKEN,
        "ARIS_AGENT_API_URL": "http://127.0.0.1:1",
        "ARIS_AGENT_SPOOL_PATH": str(path),
    }
    with collector_lock(path):
        process = subprocess.run(
            [sys.executable, "-m", "aris.apps.agent.main", "--once"],
            env=env,
            capture_output=True,
            timeout=5,
        )
    assert process.returncode == 1 and process.stdout == b""
    assert json.loads(process.stderr) == {"error_type": "CollectorAlreadyRunning"}
    assert path.read_bytes() == before and path.stat().st_ino == inode


def test_lock_survives_path_aliases_and_is_released_after_owner_process_dies(tmp_path):
    path = tmp_path / "spool.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    path.touch()
    alias.symlink_to(path)
    program = (
        "import sys; from pathlib import Path; "
        "from aris.apps.agent.lock import collector_lock; "
        "lock=collector_lock(Path(sys.argv[1])); lock.__enter__(); "
        "print('locked',flush=True); sys.stdin.read()"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", program, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(owner.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "owner failed to start"
        assert owner.stdout.readline().strip() == b"locked"
        with pytest.raises(CollectorAlreadyRunning):
            with collector_lock(alias):
                pytest.fail("same spool was acquired through a symlink")
        lock_inode = path.with_name(path.name + ".lock").stat().st_ino
    finally:
        owner.kill()
        owner.communicate(timeout=5)
    with collector_lock(alias):
        assert path.with_name(path.name + ".lock").stat().st_ino == lock_inode
    assert path.with_name(path.name + ".lock").exists()


def test_spool_health_exposes_saturation_and_reusable_pages_without_rewriting(tmp_path):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a", max_batches=4)
    observed = datetime.now(UTC) - timedelta(hours=3)
    original = spool.enqueue_resource("node-a", resource_observation(observed_at=observed))
    spool.enqueue_heartbeat("node-a", "ok")
    with sqlite3.connect(spool.path) as db:
        before = dict(db.execute("SELECT seq,payload FROM pending"))
    health = spool.statistics()
    assert health["telemetry_fill_percent"] == health["heartbeat_fill_percent"] == 25
    assert 10800 <= health["oldest_pending_age_seconds"] < 10810
    assert health["reusable_bytes"] >= 0
    assert "token" not in json.dumps(health)
    with sqlite3.connect(spool.path) as db:
        assert dict(db.execute("SELECT seq,payload FROM pending")) == before
    spool.acknowledge(original)
    assert spool.statistics()["telemetry_fill_percent"] == 0
    spool.acknowledge(spool.pending()[0])
    assert spool.statistics()["oldest_pending_age_seconds"] is None
