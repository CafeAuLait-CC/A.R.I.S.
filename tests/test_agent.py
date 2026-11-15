import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from aris.apps.agent.collector import parse_nvidia_csv
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.main import CollectionFailed, flush, main, tick
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.spool import Spool
from aris.modules.vega.schemas import Heartbeat
from tests.resource_fixtures import resource_observation


def samples():
    return parse_nvidia_csv("GPU-a, 0, NVIDIA RTX A6000, 49140, 0, 0, N/A\n")


def test_collector_handles_idle_and_unknown_temperature():
    sample = samples()[0]
    assert sample.memory_used_mib == 0
    assert sample.utilization_percent == 0
    assert sample.temperature_c is None


@pytest.mark.parametrize(
    "output", ["", "bad,csv", "GPU-a,0,RTX,49140,N/A,0,35", "GPU-a,0,RTX,49140,0,101,35"]
)
def test_collector_never_turns_missing_data_into_idle(output):
    with pytest.raises(ValueError):
        parse_nvidia_csv(output)


def test_spool_restart_sequence_and_capacity(tmp_path):
    path = tmp_path / "spool.sqlite3"
    spool = Spool(path, "agent-a", "node-a", max_batches=1)
    first = spool.enqueue_resource("host-a", resource_observation())
    restarted = Spool(path, "agent-a", "node-a", max_batches=1)
    assert restarted.pending()[0] == first
    with pytest.raises(RuntimeError, match="spool full"):
        restarted.enqueue_resource("host-a", resource_observation())
    restarted.acknowledge(first)
    assert restarted.enqueue_resource("host-a", resource_observation()).seq == 2
    with pytest.raises(ValueError, match="another agent"):
        Spool(path, "agent-b", "node-a")


def test_unsupported_pending_is_rejected_without_rewriting_or_resetting_spool(tmp_path):
    path = tmp_path / "unsupported.sqlite3"
    spool = Spool(path, "agent-a", "node-a")
    spool.enqueue_resource("host-a", resource_observation())
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX ix_pending_kind_seq")
        db.execute("UPDATE pending SET payload=?", ('{"schema_version":"aris.agent/v1"}',))
    before, inode = path.read_bytes(), path.stat().st_ino
    with pytest.raises(ValidationError):
        Spool(path, "agent-a", "node-a")
    assert path.read_bytes() == before and path.stat().st_ino == inode
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='last_seq'").fetchone()[0] == "1"


def test_collector_failure_preserves_system_and_error_heartbeat_then_recovers(
    tmp_path, monkeypatch
):
    def fail():
        raise ValueError("sensitive collector detail")

    monkeypatch.setattr("aris.apps.agent.main.collect", fail)
    spool = Spool(tmp_path / "failed.sqlite3", "agent-a", "node-a")
    settings = AgentSettings(
        _env_file=None, id="agent-a", node_id="node-a", token="test-agent-credential-12345"
    )
    requests = []

    def acknowledge(request):
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message_id": payload["message_id"],
                "duplicate": False,
                "applied_to_current": True,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(acknowledge)) as client:
        with pytest.raises(CollectionFailed):
            tick(spool, settings, client)
        assert [path for path, _ in requests] == ["/vega/heartbeat", "/vega/telemetry/batch"]
        assert requests[1][1]["resources"]["gpu"]["quality"] == "error"
        assert Heartbeat.model_validate(requests[0][1]).collector_status == "error"
        assert "sensitive" not in json.dumps(requests)
        assert spool.pending() == []
        monkeypatch.setattr("aris.apps.agent.main.collect", samples)
        assert tick(spool, settings, client) == {"seq": 4, "delivered": 2}
        assert [path for path, _ in requests[2:]] == [
            "/vega/heartbeat",
            "/vega/telemetry/batch",
        ]


def test_transport_retains_failed_and_mismatched_messages_then_replays_exactly(tmp_path):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    first = spool.enqueue_resource("host-a", resource_observation())
    settings = AgentSettings(
        _env_file=None, id="agent-a", node_id="node-a", token="test-agent-credential-12345"
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            flush(spool, settings, client)
    assert spool.pending() == [first]
    wrong = {"ok": True, "message_id": str(uuid4()), "duplicate": False, "applied_to_current": True}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=wrong))
    ) as client:
        with pytest.raises(ValueError, match="mismatched"):
            flush(spool, settings, client)
    assert spool.pending() == [first]
    correct = {**wrong, "message_id": str(first.message_id), "duplicate": True}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=correct))
    ) as client:
        assert flush(spool, settings, client) == 1
    assert spool.pending() == []


@pytest.mark.parametrize("url", ["http://remote.example.com", "https://u:p@host", "file:///tmp"])
def test_remote_transport_rejects_plaintext_and_embedded_secrets(url):
    with pytest.raises(ValidationError):
        AgentSettings(
            _env_file=None,
            id="agent-a",
            node_id="node-a",
            token="long-test-token-1234",
            api_url=url,
        )


def test_fixture_is_valid():
    from pydantic import TypeAdapter

    from aris.modules.vega.schemas import GpuSample

    assert TypeAdapter(list[GpuSample]).validate_json(Path("examples/gpu-idle.json").read_text())


def test_full_spool_can_drain_and_collection_recovers(tmp_path, monkeypatch):
    import json

    spool = Spool(tmp_path / "full.sqlite3", "agent-a", "node-a", max_batches=1)
    spool.enqueue_resource("host-a", resource_observation())
    monkeypatch.setattr("aris.apps.agent.main.collect", samples)
    settings = AgentSettings(
        _env_file=None, id="agent-a", node_id="node-a", token="test-agent-credential-12345"
    )

    def acknowledge(request):
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message_id": payload["message_id"],
                "duplicate": False,
                "applied_to_current": True,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(acknowledge)) as client:
        with pytest.raises(RuntimeError, match="spool full"):
            tick(spool, settings, client)
        assert spool.pending() == []
        assert tick(spool, settings, client) == {"seq": 4, "delivered": 2}


def test_inspection_needs_no_credentials_and_creates_no_spool_or_http_client(monkeypatch, capsys):
    import json

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not initialize credentials, spool or transport")

    monkeypatch.setattr("sys.argv", ["aris-agent", "--inspect"])
    monkeypatch.setattr("aris.apps.agent.main.ResourceSampler", lambda: ResourceSampler(samples))
    monkeypatch.setattr("aris.apps.agent.main.AgentSettings", forbidden)
    monkeypatch.setattr("aris.apps.agent.main.Spool", forbidden)
    monkeypatch.setattr("aris.apps.agent.main.httpx.Client", forbidden)
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["resources"]["gpu"]["gpus"][0]["gpu_uuid"] == "GPU-a"


def test_failed_inspection_reports_unknown_gpu_without_leaking_driver_output(monkeypatch, capsys):
    def fail():
        raise ValueError("sensitive collector detail")

    monkeypatch.setattr("sys.argv", ["aris-agent", "--inspect"])
    monkeypatch.setattr("aris.apps.agent.main.ResourceSampler", lambda: ResourceSampler(fail))
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["resources"]["gpu"]["quality"] == "error"
    assert data["resources"]["gpu"]["gpus"] == []
    assert "sensitive collector detail" not in captured.out + captured.err


def test_catch_up_is_bounded_prioritizes_current_data_and_retains_exact_history(
    tmp_path, monkeypatch
):
    spool = Spool(tmp_path / "backlog.sqlite3", "agent-a", "node-a")
    old_time = datetime.now(UTC) - timedelta(hours=12)
    for _ in range(10):
        spool.enqueue_resource("test-node", resource_observation(observed_at=old_time))
    heartbeat = spool.enqueue_heartbeat("test-node", "ok")
    with sqlite3.connect(spool.path) as db:
        before = dict(db.execute("SELECT seq,payload FROM pending"))
    monkeypatch.setattr(spool, "pending", lambda: pytest.fail("must not load an unbounded backlog"))
    settings = AgentSettings(
        _env_file=None,
        id="agent-a",
        node_id="node-a",
        token="test-agent-credential-12345",
        flush_max_messages=100,
        flush_budget_seconds=5,
    )
    clock = [10.0]
    sent = []

    def acknowledge(request):
        payload = json.loads(request.content)
        sent.append(payload)
        clock[0] += 3  # Slow transport: heartbeat/current/history reserve, then yield.
        return httpx.Response(
            200,
            json={
                "ok": True,
                "message_id": payload["message_id"],
                "duplicate": False,
                "applied_to_current": True,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(acknowledge)) as client:
        assert flush(spool, settings, client, clock=lambda: clock[0]) == 3
    assert [item["seq"] for item in sent] == [heartbeat.seq, 10, 1]
    assert sent[1]["observed_at"] == old_time.isoformat().replace("+00:00", "Z")
    with sqlite3.connect(spool.path) as db:
        after = dict(db.execute("SELECT seq,payload FROM pending"))
    assert set(after) == set(range(2, 10))
    assert all(payload == before[seq] for seq, payload in after.items())
    stats = spool.statistics()
    assert stats["pending_batches"] == 8 and stats["pending_heartbeats"] == 0
    assert stats["last_seq"] == heartbeat.seq
    assert len(spool.delivery_batch(3)) == 3


def test_post_outage_current_status_recovers_before_complete_history_drain(
    client, settings, tmp_path
):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from aris.modules.vega.models import AgentMessage
    from tests.test_telemetry import status

    spool = Spool(tmp_path / "recovery.sqlite3", "agent-a", "node-a")
    for _ in range(12):
        spool.enqueue_resource(
            "test-node", resource_observation(observed_at=datetime.now(UTC) - timedelta(minutes=10))
        )
    spool.enqueue_heartbeat("test-node", "ok")
    config = AgentSettings(
        _env_file=None,
        id="agent-a",
        node_id="node-a",
        token="test-agent-credential-12345",
        flush_max_messages=3,
    )

    def send(request):
        response = client.post(
            request.url.path,
            content=request.content,
            headers={"X-Agent-Token": "test-agent-credential-12345"},
        )
        return httpx.Response(response.status_code, json=response.json())

    with httpx.Client(transport=httpx.MockTransport(send)) as transport:
        assert flush(spool, config, transport) == 3
        node = status(client)["nodes"][0]
        assert node["status"] == "ONLINE" and node["telemetry_status"] == "STALE"
        newest = spool.enqueue_resource("test-node", resource_observation())
        spool.enqueue_heartbeat("test-node", "ok")
        assert flush(spool, config, transport) == 3
        assert spool.statistics()["pending_batches"] > 0
        assert status(client)["nodes"][0]["telemetry_status"] == "CURRENT"
        while flush(spool, config, transport):
            assert status(client)["nodes"][0][
                "observed_at"
            ] == newest.observed_at.isoformat().replace("+00:00", "Z")
    with Session(client.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(AgentMessage)) == 15
    assert spool.statistics()["pending_batches"] == 0
