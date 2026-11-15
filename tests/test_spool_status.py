import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.lock import collector_lock
from aris.apps.agent.main import main
from aris.apps.agent.spool import Spool
from aris.apps.agent.spool_status import inspect_spool
from tests.resource_fixtures import resource_observation


def test_missing_spool_remains_unknown_and_creates_nothing(tmp_path):
    path = tmp_path / "missing-parent" / "spool.sqlite3"
    status = inspect_spool(path, "agent", "node")
    assert status["status"] == "missing" and status["reason"] == "missing"
    assert status["statistics"] is None and status["estimated_seconds_without_delivery"] is None
    assert not path.parent.exists()


def test_empty_spool_reports_capacity_without_modifying_file_or_creating_lock(tmp_path):
    path = tmp_path / "empty.sqlite3"
    Spool(path, "agent", "node")
    before = path.read_bytes(), path.stat().st_ino, set(tmp_path.iterdir())
    status = inspect_spool(path, "agent", "node")
    assert status["status"] == "ok" and status["reason"] is None
    assert status["statistics"]["pending_batches"] == 0
    assert status["statistics"]["oldest_pending_age_seconds"] is None
    assert status["oldest_pending_observed_at"] is None
    assert status["remaining_sample_cycles"] == 10000
    assert status["estimated_seconds_without_delivery"] == 150000
    assert status["pending_payloads_checked"] == 0
    assert (path.read_bytes(), path.stat().st_ino, set(tmp_path.iterdir())) == before


def test_diagnostic_uses_independent_remaining_budgets_and_original_time(tmp_path):
    path = tmp_path / "state #1?.sqlite3"
    spool = Spool(path, "agent", "node")
    now = datetime.now(UTC)
    observed = now - timedelta(hours=3)
    spool.enqueue_resource("node", resource_observation(observed_at=observed))
    spool.enqueue_resource("node", resource_observation())
    spool.enqueue_heartbeat("node", "ok")
    before = path.read_bytes(), path.stat().st_ino
    status = inspect_spool(
        path, "agent", "node", max_batches=10, max_heartbeats=3, interval_seconds=30, now=now
    )
    assert status["status"] == "ok"
    assert status["statistics"]["pending_batches"] == 2
    assert status["statistics"]["pending_heartbeats"] == 1
    assert status["statistics"]["last_seq"] == 3
    assert status["statistics"]["oldest_pending_age_seconds"] == 10800
    assert status["oldest_pending_observed_at"] == observed.isoformat()
    assert status["remaining_sample_cycles"] == 2
    assert status["estimated_seconds_without_delivery"] == 60
    assert status["pending_payloads_checked"] == 1
    assert (path.read_bytes(), path.stat().st_ino) == before
    lowered = inspect_spool(path, "agent", "node", max_batches=1)
    assert lowered["statistics"]["telemetry_fill_percent"] == 200
    assert lowered["remaining_sample_cycles"] == 0


def test_diagnostic_does_not_take_or_replace_running_collectors_lock(tmp_path):
    path = tmp_path / "spool.sqlite3"
    Spool(path, "agent", "node")
    with collector_lock(path):
        lock = path.with_name(path.name + ".lock")
        before = path.read_bytes(), lock.stat().st_ino
        assert inspect_spool(path, "agent", "node")["status"] == "ok"
        assert (path.read_bytes(), lock.stat().st_ino) == before


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DROP TABLE metadata", "invalid_schema"),
        ("UPDATE metadata SET value='-1' WHERE key='last_seq'", "invalid_data"),
        ("UPDATE metadata SET value='0' WHERE key='last_seq'", "invalid_data"),
        ("UPDATE pending SET kind='future-kind'", "invalid_data"),
        ("UPDATE pending SET payload='{private-corrupt-content}'", "invalid_data"),
    ],
)
def test_incompatible_spools_are_reported_without_repair_or_data_leak(tmp_path, sql, reason):
    path = tmp_path / "invalid.sqlite3"
    spool = Spool(path, "agent", "node")
    spool.enqueue_heartbeat("node", "ok")
    with sqlite3.connect(path) as db:
        db.execute(sql)
    before = path.read_bytes(), path.stat().st_ino
    status = inspect_spool(path, "agent", "node")
    assert status["status"] == "unknown" and status["reason"] == reason
    assert status["statistics"] is None and status["remaining_sample_cycles"] is None
    assert "private-corrupt-content" not in json.dumps(status)
    assert (path.read_bytes(), path.stat().st_ino) == before


def test_wrong_identity_and_non_sqlite_file_remain_unchanged(tmp_path):
    path = tmp_path / "private.sqlite3"
    Spool(path, "private-agent", "private-node")
    before = path.read_bytes()
    status = inspect_spool(path, "other-agent", "other-node")
    assert status["reason"] == "identity_mismatch" and status["statistics"] is None
    assert "private-agent" not in json.dumps(status) and path.read_bytes() == before
    path.write_bytes(b"not-a-database-with-private-value")
    status = inspect_spool(path, "agent", "node")
    assert status["reason"] == "invalid_data"
    assert path.read_bytes() == b"not-a-database-with-private-value"


def test_busy_writer_is_not_interrupted_or_reported_as_empty(tmp_path):
    path = tmp_path / "busy.sqlite3"
    Spool(path, "agent", "node")
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        status = inspect_spool(path, "agent", "node")
        assert status["reason"] == "busy"
        assert status["statistics"] is None and status["estimated_seconds_without_delivery"] is None
        writer.execute("SELECT count(*) FROM pending").fetchone()


def test_wal_is_rejected_before_opening_sqlite_or_creating_sidecars(tmp_path):
    path = tmp_path / "wal.sqlite3"
    Spool(path, "agent", "node")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = path.read_bytes(), set(tmp_path.iterdir())
    assert inspect_spool(path, "agent", "node")["reason"] == "unsupported_journal_mode"
    assert (path.read_bytes(), set(tmp_path.iterdir())) == before


def test_status_cli_never_samples_sends_or_initializes_spool(tmp_path, monkeypatch, capsys):
    path = tmp_path / "spool.sqlite3"
    Spool(path, "agent", "node")
    config = AgentSettings(
        _env_file=None,
        id="agent",
        node_id="node",
        token="private-test-token-12345",
        spool_path=path,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic tried an operation beyond read-only inspection")

    monkeypatch.setattr("sys.argv", ["aris-vega-collector", "--spool-status"])
    monkeypatch.setattr("aris.apps.agent.main.AgentSettings", lambda: config)
    for name in ("ResourceSampler", "Spool", "collector_lock", "DeliveryWorker", "LiveClient"):
        monkeypatch.setattr("aris.apps.agent.main." + name, forbidden)
    monkeypatch.setattr("aris.apps.agent.main.httpx.Client", forbidden)
    main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "ok" and captured.err == ""
    assert config.token.get_secret_value() not in captured.out
    monkeypatch.setattr("sys.argv", ["aris-vega-collector", "--spool-status", "--once"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_status_cli_missing_and_configuration_failure_do_not_fake_success(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr("sys.argv", ["aris-vega-collector", "--spool-status"])
    config = AgentSettings(
        _env_file=None,
        id="agent",
        node_id="node",
        token="private-test-token-12345",
        spool_path=tmp_path / "missing.sqlite3",
    )
    monkeypatch.setattr("aris.apps.agent.main.AgentSettings", lambda: config)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1 and json.loads(capsys.readouterr().out)["status"] == "missing"

    def invalid_config():
        raise ValueError("private-token-and-URL-must-not-be-printed")

    monkeypatch.setattr("aris.apps.agent.main.AgentSettings", invalid_config)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "unknown", "error_type": "ValueError"}
    assert "private-token" not in captured.out + captured.err
