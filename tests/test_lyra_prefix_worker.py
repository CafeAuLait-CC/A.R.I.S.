import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from aris.apps.composition.lyra_prefix import (
    LyraPrefixRuntimeConfig,
    build_lyra_prefix_components,
    load_lyra_prefix_config,
)
from aris.apps.worker.lyra_prefix import run_lyra_prefix_worker
from aris.modules.lyra.models import SourcePrefixCheckpointRow
from tests.test_administration import NOW
from tests.test_source_prefix_checkpoints import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    prefix_service,
    stable_recording,
)

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_recording",
]


def runtime(state, limit=1):
    service = prefix_service(state[0])
    return LyraPrefixRuntimeConfig(
        enabled=True,
        configuration=service.configuration,
        before=NOW + timedelta(seconds=116),
        max_proofs_per_tick=limit,
        poll_seconds=0.1,
    ), service


def private_file(path, config):
    path.write_text(config.model_dump_json())
    path.chmod(0o600)
    return path


def count(service):
    with service.sessions() as db:
        return db.scalar(select(func.count()).select_from(SourcePrefixCheckpointRow))


def process(path, service, *arguments):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))}
    env.update(
        ARIS_ENVIRONMENT="test",
        ARIS_DATABASE_URL=service.sessions.kw["bind"].url.render_as_string(hide_password=False),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "aris.apps.worker.main",
            "--lyra-prefix-config",
            str(path),
            *arguments,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("limit,expected_count,eligible_revision", [(1, 3, 2), (8, 1, None)])
def test_maintenance_restarts_stop_at_boundary_and_never_offer_the_ineligible_tail(
    stable_recording,
    limit,
    expected_count,
    eligible_revision,
):
    config, service = runtime(stable_recording, limit)
    report = None
    for _ in range(5):
        graph = build_lyra_prefix_components(service.sessions, config, clock=service.clock)
        report = graph.run_once()
        assert not report["source_deletion_enabled"] and not report["quota_usage_available"]
        if report["state"] == "boundary_reached":
            break
    assert report["state"] == "boundary_reached" and count(service) == expected_count
    eligible = report["status"]["eligible"]
    assert (eligible["through_revision"] if eligible else None) == eligible_revision
    assert report["status"]["latest"]["through_revision"] == 3
    paused = config.model_copy(update={"enabled": False})
    reading = build_lyra_prefix_components(
        service.sessions, paused, clock=service.clock, status_only=True
    )
    assert reading.run_once(status_only=True)["status"] == report["status"]
    assert count(service) == expected_count


def test_real_cli_once_then_finite_daemon_reuses_saved_progress_and_status_is_read_only(
    stable_recording,
    tmp_path,
):
    config, service = runtime(stable_recording)
    path = private_file(tmp_path / "prefix.json", config)
    status = process(path, service, "--prefix-status")
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "pending"
    assert count(service) == 0
    lag = (service.clock() - datetime.now(UTC)).total_seconds()
    assert lag < 2
    if lag > 0:
        time.sleep(lag + 0.01)
    first = process(path, service, "--once")
    assert (
        first.returncode == 0
        and json.loads(first.stdout)["status"]["latest"]["through_revision"] == 1
    )
    completed = process(path, service)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [line["state"] for line in lines] == ["advanced", "advanced", "boundary_reached"]
    assert lines[-1]["status"]["eligible"]["through_revision"] == 2
    before = count(service)
    assert process(path, service, "--once").returncode == 0 and count(service) == before
    # Another explicit cutoff can reuse all verified source without writing any new checkpoint.
    later = config.model_copy(update={"before": NOW + timedelta(seconds=200), "enabled": False})
    private_file(path, later)
    status = process(path, service, "--prefix-status")
    assert status.returncode == 0 and json.loads(status.stdout)["state"] == "caught_up"
    assert json.loads(status.stdout)["status"]["eligible"]["through_revision"] == 3
    assert count(service) == before


def test_disabled_prefix_worker_never_reads_settings_or_opens_database(monkeypatch, capsys):
    import aris.apps.worker.lyra_prefix as module

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled prefix accessed DB")

    monkeypatch.setattr(module, "Settings", forbidden)
    graph = build_lyra_prefix_components(forbidden, LyraPrefixRuntimeConfig())
    assert graph.run_once()["state"] == "paused"
    run_lyra_prefix_worker(LyraPrefixRuntimeConfig(), once=True)
    assert json.loads(capsys.readouterr().out)["state"] == "paused"


def test_prefix_configuration_is_private_and_has_no_guessed_source_or_time(tmp_path):
    path = private_file(tmp_path / "prefix.json", LyraPrefixRuntimeConfig())
    assert not load_lyra_prefix_config(path).enabled
    with pytest.raises(ValidationError):
        LyraPrefixRuntimeConfig(enabled=True)
    for raw in ('{"enabled":true,"enabled":false}', '{"before":"2026-01-01T00:00:00"}'):
        path.write_text(raw)
        with pytest.raises(ValueError):
            load_lyra_prefix_config(path)
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_lyra_prefix_config(path)
    path.chmod(0o600)
    link = tmp_path / "linked"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_lyra_prefix_config(link)


def test_prefix_cli_cannot_be_mixed_with_another_worker_or_orphan_status(monkeypatch, tmp_path):
    import aris.apps.worker.main as module

    path = private_file(tmp_path / "prefix.json", LyraPrefixRuntimeConfig())
    for other in (
        ["--slack"],
        ["--lyra-config", str(path)],
        ["--lyra-coverage-config", str(path)],
        ["--coverage-status"],
        ["--m2-mode", "all"],
        ["--m2-config", str(path)],
    ):
        monkeypatch.setattr(sys, "argv", ["worker", "--lyra-prefix-config", str(path), *other])
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 2
    monkeypatch.setattr(sys, "argv", ["worker", "--prefix-status"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
