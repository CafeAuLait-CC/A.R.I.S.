import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from aris.apps.composition.lyra import LyraRuntimeConfig
from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
    load_lyra_coverage_config,
)
from aris.apps.worker.lyra import run_lyra_worker
from aris.apps.worker.lyra_coverage import run_lyra_coverage_worker
from aris.core.config import Settings
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from tests.test_administration import NOW
from tests.test_coverage_builds import build_request, build_service, counts
from tests.test_heartbeat import beat, heartbeat
from tests.test_stable_coverage import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    open_reference,
    partition_recording,
    post_remaining,
    service_for,
    stable_recording,
)
from tests.test_usage_ledger import POST

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_recording",
]


def runtime(stable_recording):
    writer, closed, (app, _, _) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    post_remaining(writer, closed)
    coverage = service_for(writer)
    target = build_request(build_service(coverage)).query
    return LyraCoverageRuntimeConfig(
        enabled=True,
        configuration=coverage.configuration,
        target=CoverageBuildTarget(id=uuid4(), query=target),
        poll_seconds=0.1,
    ), coverage


def private_config(path, config):
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    return path


def process(config, coverage, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))}
    env.update(
        ARIS_ENVIRONMENT="test",
        ARIS_DATABASE_URL=coverage.sessions.kw["bind"].url.render_as_string(hide_password=False),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "aris.apps.worker.main",
            "--lyra-coverage-config",
            str(config),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_disabled_coverage_and_stable_recording_modes_never_open_a_database(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled worker accessed settings or database")

    import aris.apps.worker.lyra as recording
    import aris.apps.worker.lyra_coverage as coverage

    monkeypatch.setattr(recording, "Settings", forbidden)
    monkeypatch.setattr(coverage, "Settings", forbidden)
    graph = build_lyra_coverage_components(
        forbidden, LyraCoverageRuntimeConfig(), clock=lambda: NOW
    )
    assert graph.run_once()["state"] == "paused"
    run_lyra_coverage_worker(LyraCoverageRuntimeConfig(), once=True)
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "paused" and not result["quota_usage_available"]
    run_lyra_worker(
        LyraRuntimeConfig(method=StableResidencyLayoutMethod(max_intervals=4)), once=True
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "lyra-recording-stable"


def test_private_config_requires_explicit_target_and_rejects_unsafe_files(tmp_path):
    path = private_config(tmp_path / "coverage.json", LyraCoverageRuntimeConfig())
    assert not load_lyra_coverage_config(path).enabled
    with pytest.raises(ValidationError):
        LyraCoverageRuntimeConfig(enabled=True)
    for raw in ('{"enabled":false,"enabled":true}', '{"enabled":"false"}', '{"unknown":0}'):
        path.write_text(raw)
        with pytest.raises(ValueError):
            load_lyra_coverage_config(path)
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_lyra_coverage_config(path)
    path.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_lyra_coverage_config(link)
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe, 0o600)
    with pytest.raises(ValueError):
        load_lyra_coverage_config(pipe)
    path.write_text(" " * 65537)
    with pytest.raises(ValueError):
        load_lyra_coverage_config(path)


def test_real_cli_once_restarts_and_finite_daemon_keep_one_fixed_window(stable_recording, tmp_path):
    config, coverage = runtime(stable_recording)
    path = private_config(tmp_path / "coverage.json", config)
    build = build_service(coverage)
    before = counts(build)
    status = process(path, coverage, "--coverage-status")
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "not_started"
    assert counts(build) == before
    # The shared fixture saves source proofs one second ahead of wall time.
    # Wait for that actual deadline before asserting the daemon's exact successful phases.
    lag = (coverage.clock() - datetime.now(UTC)).total_seconds()
    assert lag < 2
    if lag > 0:
        time.sleep(lag + 0.01)
    completed = process(path, coverage)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    lines = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [line["state"] for line in lines] == [
        "admitted",
        "source_built",
        "source_built",
        "source_built",
        "finished",
    ]
    assert lines[-1]["coverage"] == "complete" and all(
        not line["quota_usage_available"] for line in lines
    )
    prior = counts(build)
    assert process(path, coverage, "--once").returncode == 0 and counts(build) == prior
    # A separate explicit UUID admits a new view. Restarts keep its old source despite new data.
    config = config.model_copy(update={"target": config.target.model_copy(update={"id": uuid4()})})
    private_config(path, config)
    first = process(path, coverage, "--once")
    assert first.returncode == 0 and json.loads(first.stdout)["state"] == "admitted"
    assert beat(stable_recording[2][2][2][4], heartbeat(seq=25)).status_code == 200
    for _ in range(4):
        result = process(path, coverage, "--once")
        assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["state"] == "finished" and report["progress"]["processed_revision"] == 3
    saved = build.windows.exact(report["snapshot_id"])
    assert saved.window.source.highest_committed_seq == 24
    assert saved.window.total_usage_microseconds == 135_000_000
    # Reverify through the same wall-clock composition as the child, rather than its frozen fixture.
    reading = build_lyra_coverage_components(coverage.sessions, config)
    assert reading.service.windows.reverify(saved.request.id) == saved
    prior = counts(build)
    paused = config.model_copy(update={"enabled": False})
    private_config(path, paused)
    status = process(path, coverage, "--coverage-status")
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "finished"
    assert counts(build) == prior
    changed = config.model_copy(
        update={
            "target": config.target.model_copy(
                update={"query": config.target.query.model_copy(update={"owner_id": uuid4()})}
            )
        }
    )
    private_config(path, changed)
    rejected = process(path, coverage, "--once")
    assert rejected.returncode == 1 and json.loads(rejected.stdout)["code"] == "CONFLICT"
    assert counts(build) == prior


def test_missing_or_old_schema_is_rejected_without_creating_or_migrating(
    stable_recording, tmp_path
):
    config, coverage = runtime(stable_recording)
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(SystemExit):
        run_lyra_coverage_worker(
            config,
            once=True,
            settings=Settings(
                _env_file=None, environment="test", database_url=f"sqlite+pysqlite:///{missing}"
            ),
        )
    assert not missing.exists()
    command.downgrade(Config("alembic.ini"), "0039")
    path = private_config(tmp_path / "coverage.json", config)
    result = process(path, coverage, "--once")
    assert result.returncode == 1 and json.loads(result.stdout)["state"] == "failed"
    # The read guard must not silently install the required revision.
    from sqlalchemy import text

    with coverage.sessions() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0039"


def test_coverage_worker_argument_mode_is_exclusive(monkeypatch, tmp_path):
    import aris.apps.worker.main as module

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid arguments ran another worker")

    monkeypatch.setattr(module, "run_lyra_coverage_worker", forbidden)
    path = private_config(tmp_path / "coverage.json", LyraCoverageRuntimeConfig())
    for other in (
        ["--slack"],
        ["--lyra-config", str(path)],
        ["--m2-mode", "all"],
        ["--m2-config", str(path)],
    ):
        monkeypatch.setattr(sys, "argv", ["worker", "--lyra-coverage-config", str(path), *other])
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 2
    monkeypatch.setattr(sys, "argv", ["worker", "--coverage-status"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
