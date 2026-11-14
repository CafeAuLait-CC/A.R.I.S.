import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from aris.apps.agent.collector import parse_nvidia_csv
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.main import CollectionFailed, flush, main, tick
from aris.apps.agent.spool import Spool
from aris.modules.vega.schemas import Heartbeat


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
    first = spool.enqueue("host-a", samples())
    restarted = Spool(path, "agent-a", "node-a", max_batches=1)
    assert restarted.pending()[0] == first
    with pytest.raises(RuntimeError, match="spool full"):
        restarted.enqueue("host-a", samples())
    restarted.acknowledge(first)
    assert restarted.enqueue("host-a", samples()).seq == 2
    with pytest.raises(ValueError, match="another agent"):
        Spool(path, "agent-b", "node-a")


def test_existing_spool_upgrades_without_changing_unacknowledged_messages(tmp_path):
    path = tmp_path / "old.sqlite3"
    original = Spool(path, "agent-a", "node-a").enqueue("host-a", samples())
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE pending DROP COLUMN kind")
        before = json.dumps(original.model_dump(mode="json"), sort_keys=True)
        db.execute("UPDATE pending SET payload=?", (before,))
    spool = Spool(path, "agent-a", "node-a", max_batches=1)
    heartbeat = spool.enqueue_heartbeat("host-a", "error")
    assert heartbeat.seq == 2
    with pytest.raises(RuntimeError, match="spool full"):
        spool.enqueue_heartbeat("host-a", "ok")
    restarted = Spool(path, "agent-a", "node-a")
    assert restarted.pending() == [original, heartbeat]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT payload FROM pending WHERE seq=1").fetchone()[0] == before
    restarted.acknowledge(original)
    assert restarted.pending() == [heartbeat]


def test_collector_failure_sends_only_error_heartbeat_and_keeps_sequence(tmp_path, monkeypatch):
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
        with pytest.raises(CollectionFailed, match="^ValueError$"):
            tick(spool, settings, client)
        assert requests[0][0] == "/vega/v1/heartbeat"
        assert Heartbeat.model_validate(requests[0][1]).collector_status == "error"
        assert "sensitive" not in json.dumps(requests)
        assert spool.pending() == []
        monkeypatch.setattr("aris.apps.agent.main.collect", samples)
        assert tick(spool, settings, client) == {"seq": 3, "delivered": 2}
        assert [path for path, _ in requests[1:]] == [
            "/vega/v1/telemetry/batch",
            "/vega/v1/heartbeat",
        ]


def test_transport_retains_failed_and_mismatched_messages_then_replays_exactly(tmp_path):
    spool = Spool(tmp_path / "spool.sqlite3", "agent-a", "node-a")
    first = spool.enqueue("host-a", samples())
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
    spool.enqueue("host-a", samples())
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
    monkeypatch.setattr("aris.apps.agent.main.collect", samples)
    monkeypatch.setattr("aris.apps.agent.main.AgentSettings", forbidden)
    monkeypatch.setattr("aris.apps.agent.main.Spool", forbidden)
    monkeypatch.setattr("aris.apps.agent.main.httpx.Client", forbidden)
    main()
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "aris.agent-inspection/v1"
    assert output["gpus"][0]["gpu_uuid"] == "GPU-a"


def test_failed_inspection_returns_error_without_an_empty_or_idle_snapshot(monkeypatch, capsys):
    def fail():
        raise ValueError("sensitive collector detail")

    monkeypatch.setattr("sys.argv", ["aris-agent", "--inspect"])
    monkeypatch.setattr("aris.apps.agent.main.collect", fail)
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ValueError" in captured.err
    assert "sensitive collector detail" not in captured.err
