import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, text

from aris.apps.api.main import create_app
from aris.apps.composition.lyra import (
    MAX_LYRA_RUNTIME_BYTES,
    LyraRuntimeConfig,
    LyraSourceBinding,
    build_lyra_components,
    load_lyra_config,
)
from aris.apps.worker.lyra import run_lyra_worker
from aris.core.config import Settings
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from tests.conftest import READ_TOKEN
from tests.test_administration import NOW
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_mapping_history import assign
from tests.test_partition_recording import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
)
from tests.test_resource_telemetry import put, resource_batch

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


def configuration(state):
    writer, request, _, _, _ = state
    trace = writer.evidence.records.configuration
    return LyraRuntimeConfig(
        enabled=True,
        source_ref="private-runtime-fixture-context",
        recording=writer.ledger.configuration,
        continuity=trace.continuity,
        formation=trace.formation,
        method=writer.evidence.method,
        identity_revision=request.identity_revision,
        sources=(
            LyraSourceBinding(
                consumer_id="runtime-source-a",
                agent_id="agent-a",
                node_id="node-a",
                initial_after_seq=2,
            ),
        ),
        jobs=RecordingJobLimits(
            transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
        ),
        source_limit=8,
        dispatch_limit=1,
    )


def private_file(tmp_path, config):
    path = tmp_path / "lyra-private.json"
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    return path


def process(path, state, **environment):
    url = state[0].sessions.kw["bind"].url.render_as_string(hide_password=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))}
    env.update(ARIS_ENVIRONMENT="test", ARIS_DATABASE_URL=url, **environment)
    return subprocess.run(
        [sys.executable, "-m", "aris.apps.worker.main", "--lyra-config", str(path), "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_default_disabled_graph_and_worker_do_not_access_database_or_settings(
    partition_recording, tmp_path, monkeypatch
):
    state = partition_recording
    disabled = LyraRuntimeConfig()
    statements = []
    engine = state[0].sessions.kw["bind"]

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        graph = build_lyra_components(state[0].sessions, disabled)
        assert graph.pump is None and graph.run_once()["paused"]
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements == []
    path = private_file(tmp_path, disabled)
    result = process(path, state, ARIS_AGENT_CREDENTIALS="invalid-private-credential-input")
    assert result.returncode == 0 and json.loads(result.stdout)["paused"]
    assert "invalid-private-credential-input" not in result.stdout + result.stderr


def test_runtime_context_cannot_enable_with_guessed_timing_identity_or_duplicate_sources(
    partition_recording,
):
    with pytest.raises(ValidationError):
        LyraRuntimeConfig(enabled=True)
    config = configuration(partition_recording)
    for changes in (
        {
            "recording": config.recording.model_copy(
                update={"timing_fingerprint": "f" * 64}
            ).model_dump(mode="json")
        },
        {"sources": [source.model_dump(mode="json") for source in config.sources] * 2},
        {"grant_admin": True},
        {"method": {"max_intervals": 1}},
    ):
        with pytest.raises(ValidationError):
            LyraRuntimeConfig.model_validate({**config.model_dump(mode="json"), **changes})


def test_private_loader_rejects_public_symlink_fifo_large_and_duplicate_key_input(tmp_path):
    path = private_file(tmp_path, LyraRuntimeConfig())
    assert load_lyra_config(path) == LyraRuntimeConfig()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_lyra_config(path)
    path.chmod(0o600)
    link = tmp_path / "config-link.json"
    link.symlink_to(path)
    with pytest.raises((ValueError, OSError)):
        load_lyra_config(link)
    fifo = tmp_path / "config-pipe"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError):
        load_lyra_config(fifo)
    path.write_bytes(b"x" * (MAX_LYRA_RUNTIME_BYTES + 1))
    with pytest.raises(ValueError):
        load_lyra_config(path)
    path.write_text('{"enabled":false,"enabled":true}')
    with pytest.raises(ValueError, match="duplicate"):
        load_lyra_config(path)


def test_real_once_process_restarts_preserve_sources_plans_measurements_and_private_output(
    partition_recording, tmp_path
):
    state = partition_recording
    config = configuration(state)
    path = private_file(tmp_path, config)
    reports = []
    for _ in range(5):
        result = process(path, state)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert not report["failures"] and not report["quota_usage_available"]
        assert "linux-user" not in result.stdout + result.stderr
        assert config.source_ref not in result.stdout + result.stderr
        reports.append(report)
    assert [value["source"]["after_seq"] for value in reports] == [10, 18, 24, 24, 24]
    assert [value["job"]["state"] if value["job"] else None for value in reports] == [
        None,
        None,
        "completed",
        "completed",
        "completed",
    ]
    with state[0].sessions() as session:
        assert (
            session.scalar(text("SELECT sum(observed_microseconds) FROM usage_ledger_entries"))
            == 135000000
        )
        assert session.scalar(text("SELECT count(*) FROM usage_recording_plans")) == 1
    assert all(value["configuration_fingerprint"] == config.fingerprint for value in reports)


def test_generic_runtime_composition_handles_two_distinct_logical_nodes_in_one_once_cycle(
    partition_recording, tmp_path
):
    state = partition_recording
    history = state[2][2][2]
    token = "fixture-runtime-second-node-token-0000"
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=state[0].sessions.kw["bind"].url.render_as_string(hide_password=False),
        read_token=READ_TOKEN,
        agent_credentials={"agent-node-b": {"node_id": "node-b", "token": token}},
    )

    def node_b(payload):
        payload.update(agent_id="agent-node-b", node_id="node-b", hostname="runtime-fixture-b")
        if "resources" in payload:
            payload["resources"]["gpu"]["gpus"][0]["gpu_uuid"] = "GPU-runtime-b"
            for gpu in payload["resources"]["compute"]["gpus"]:
                gpu["gpu_uuid"] = "GPU-runtime-b"
        return payload

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        for index in range(3):
            payload = node_b(
                resource_batch(
                    seq=2 + 2 * index, observed_at=NOW + timedelta(seconds=10 + 15 * index)
                )
            )
            payload = ownership(
                payload,
                [
                    owner(
                        username="linux-user",
                        gpus=[{"gpu_uuid": "GPU-runtime-b", "reported_processes": 1}],
                    )
                ],
            )
            assert put(client, payload, token=token).status_code == 200
            assert (
                beat(client, node_b(heartbeat(seq=3 + 2 * index)), token=token).status_code == 200
            )
        idle = node_b(resource_batch(seq=8, observed_at=NOW + timedelta(seconds=55)))
        idle["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert (
            put(client, idle, token=token).status_code
            == beat(client, node_b(heartbeat(seq=9)), token=token).status_code
            == 200
        )
    mapped = assign(history, 0, node="node-b")
    config = configuration(state).model_copy(
        update={
            "identity_revision": mapped.revision,
            "sources": (
                *configuration(state).sources,
                LyraSourceBinding(
                    consumer_id="runtime-source-b",
                    agent_id="agent-node-b",
                    node_id="node-b",
                    initial_after_seq=1,
                ),
            ),
        }
    )
    result = process(private_file(tmp_path, config), state)
    assert result.returncode == 0, result.stdout + result.stderr
    reports = [json.loads(line) for line in result.stdout.splitlines()]
    assert [value["source"]["consumer_id"] for value in reports] == [
        "runtime-source-a",
        "runtime-source-b",
    ]
    state[4]["now"] = max(datetime.now(UTC), state[4]["now"]) + timedelta(seconds=1)
    graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    for _ in range(6):
        assert not graph.run_once()["failures"]
    with state[0].sessions() as session:
        totals = dict(
            session.execute(
                text(
                    "SELECT node_id,sum(observed_microseconds) "
                    "FROM usage_ledger_entries GROUP BY node_id"
                )
            )
            .tuples()
            .all()
        )
    assert totals == {"node-a": 135000000, "node-b": 30000000}


def test_once_visits_other_configured_sources_after_an_unregistered_source_fails(
    partition_recording, tmp_path
):
    state = partition_recording
    config = configuration(state)
    unknown = LyraSourceBinding(
        consumer_id="unknown-runtime-source",
        agent_id="agent-unknown",
        node_id="node-unknown",
        initial_after_seq=0,
    )
    config = config.model_copy(update={"sources": (unknown, *config.sources)})
    result = process(private_file(tmp_path, config), state)
    reports = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1 and len(reports) == 2
    assert reports[0]["failures"][0]["code"] == "NOT_FOUND"
    assert reports[1]["source"]["after_seq"] == 10 and not reports[1]["failures"]


def test_wrong_database_revision_is_rejected_without_migration_or_initialization(
    partition_recording, tmp_path
):
    state = partition_recording
    with state[0].sessions.begin() as session:
        session.execute(text("UPDATE alembic_version SET version_num='0019'"))
    result = process(private_file(tmp_path, configuration(state)), state)
    assert result.returncode == 1 and json.loads(result.stdout)["error_type"] == "RuntimeError"
    with state[0].sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0019"
        assert session.scalar(text("SELECT count(*) FROM usage_ledger_entries")) == 0
        assert session.scalar(text("SELECT count(*) FROM usage_recording_plans")) == 0


def test_enabled_worker_does_not_create_a_missing_test_database(
    partition_recording, tmp_path, capsys
):
    missing = tmp_path / "must-not-be-created.sqlite"
    settings = Settings(
        _env_file=None, environment="test", database_url="sqlite+pysqlite:///" + str(missing)
    )
    with pytest.raises(SystemExit) as error:
        run_lyra_worker(configuration(partition_recording), True, settings=settings)
    assert error.value.code == 1 and not missing.exists()
    assert json.loads(capsys.readouterr().out)["error_type"] == "ValueError"


def test_startup_error_output_does_not_include_private_values(
    partition_recording, monkeypatch, capsys
):
    import aris.apps.worker.lyra as module

    secret = "fixture-secret-that-must-not-enter-output"

    def failed(*args):
        raise RuntimeError(secret)

    monkeypatch.setattr(module, "build_engine", failed)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+psycopg://private:password@unused.invalid/test",
    )
    with pytest.raises(SystemExit):
        run_lyra_worker(configuration(partition_recording), True, settings=settings)
    output = capsys.readouterr()
    assert secret not in output.out + output.err and "password" not in output.out + output.err
    assert json.loads(output.out)["error_type"] == "RuntimeError"


def test_worker_mode_arguments_are_mutually_exclusive_before_loading_config():
    for arguments in (
        ["--slack"],
        ["--m2-mode", "commands"],
        ["--m2-config", "missing.json", "--m2-mode", "commands"],
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "aris.apps.worker.main",
                "--lyra-config",
                "missing.json",
                *arguments,
                "--once",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2 and "cannot be combined" in result.stderr
