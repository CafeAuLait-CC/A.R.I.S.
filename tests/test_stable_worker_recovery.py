import json
import os
import subprocess
import sys
import time
from uuid import UUID

import pytest
from sqlalchemy import text

from aris.apps.composition.lyra import LyraRuntimeConfig, build_lyra_components
from aris.core.contracts import DomainError
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from tests.test_lyra_composition import private_file, process
from tests.test_recording_pump import recorded
from tests.test_stable_recording_worker import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    stable_config,
)

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]
pytestmark = pytest.mark.skipif(
    not os.environ.get("ARIS_TEST_DATABASE_URL"),
    reason="real process recovery integration runs on owned PostgreSQL",
)

_CRASH_WRAPPER = """
import importlib
import json
import os
from pathlib import Path
import runpy
import signal
import sys

stage, when, marker, *arguments = sys.argv[1:]
module = importlib.import_module({
    "source": "aris.modules.lyra.stable_pipeline_service",
    "dispatch": "aris.modules.lyra.stable_dispatch_service",
    "execute": "aris.modules.lyra.recording_job_service",
}[stage])
original = module.commit_owned_transaction

def commit(session):
    targeted = stage != "execute" or any(
        getattr(row, "__tablename__", None) == "usage_recording_jobs"
        and row.state == "completed" for row in session.identity_map.values()
    )
    if not targeted:
        return original(session)
    if when == "after":
        original(session)
    Path(marker).write_text(json.dumps({"stage": stage, "when": when}))
    os.kill(os.getpid(), signal.SIGKILL)

module.commit_owned_transaction = commit
sys.argv = ["aris.apps.worker.main", *arguments]
runpy.run_module("aris.apps.worker.main", run_name="__main__")
"""


@pytest.mark.parametrize("stage", ["source", "dispatch", "execute"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_real_worker_sigkill_before_and_after_each_commit_recovers_exactly_once(
    partition_recording, tmp_path, stage, when
):
    state = partition_recording
    config = stable_config(state).model_copy(
        update={
            "jobs": RecordingJobLimits(
                transaction_timeout_ms=5000,
                lease_seconds=8,
                max_attempts=3,
                retry_base_seconds=1,
            )
        }
    )
    path = private_file(tmp_path, config)
    first = process(path, state)
    assert first.returncode == 0 and json.loads(first.stdout)["source"]["after_seq"] == 10
    wrapper, marker = tmp_path / "crash_worker.py", tmp_path / "crash_marker.json"
    wrapper.write_text(_CRASH_WRAPPER)
    wrapper.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))}
    env.update(
        ARIS_ENVIRONMENT="test",
        ARIS_DATABASE_URL=state[0].sessions.kw["bind"].url.render_as_string(hide_password=False),
    )
    killed = subprocess.run(
        [
            sys.executable,
            str(wrapper),
            stage,
            when,
            str(marker),
            "--lyra-config",
            str(path),
            "--once",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert killed.returncode == -9, killed.stdout + killed.stderr
    assert json.loads(marker.read_text()) == {"stage": stage, "when": when}
    if stage == "execute" and when == "after":
        assert recorded(state[0]) == (1, 45000000)
    elif stage == "execute":
        assert recorded(state[0]) == (0, 0)
    deadline = time.monotonic() + 20
    while recorded(state[0]) != (3, 135000000) and time.monotonic() < deadline:
        result = process(path, state)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert not report["failures"]
        if report["job"] is None:
            time.sleep(0.1)
    assert recorded(state[0]) == (3, 135000000)
    with state[0].sessions() as db:
        assert (
            db.scalar(text("SELECT count(*) FROM usage_recording_jobs WHERE state<>'completed'"))
            == 0
        )
        assert db.scalar(text("SELECT count(*) FROM usage_stable_window_acceptances")) == 3
        assert db.scalar(text("SELECT count(*) FROM usage_ledger_operations")) == 3
        assert (
            db.scalar(
                text(
                    "SELECT after_seq FROM usage_trace_cursors WHERE consumer_id='runtime-source-a'"
                )
            )
            == 24
        )


@pytest.mark.parametrize("partition_recording", [1030], indirect=True)
def test_composed_worker_records_long_live_prefixes_and_final_tail_with_bounded_raw_pages(
    partition_recording,
    monkeypatch,
):
    state = partition_recording
    config = LyraRuntimeConfig.model_validate(
        {
            **stable_config(state).model_dump(),
            "method": StableResidencyLayoutMethod(max_intervals=8),
            "source_limit": 128,
        }
    )
    graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    recorder = graph.pump.jobs.stable_recorder
    record_in_transaction = recorder.record_in_transaction
    injected = False

    def retry_first(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Fixture recorder timed out", 503)
        return record_in_transaction(*args, **kwargs)

    monkeypatch.setattr(recorder, "record_in_transaction", retry_first)
    reports = []
    for _ in range(160):
        report = graph.run_once()
        assert not report["failures"]
        reports.append(report)
        job = report["job"]
        if job is not None and job["state"] == "retry":
            pending = graph.pump.jobs.exact(UUID(job["id"]))
            assert pending.state == "queued"
            state[4]["now"] = max(state[4]["now"], pending.available_at)
        if (
            report["source"]["state"] == "caught_up"
            and report["dispatch"]["state"] == "idle"
            and job is None
        ):
            break
    assert len(reports) <= 132
    assert injected
    assert any(report["job"] and report["job"]["state"] == "retry" for report in reports)
    assert recorded(state[0]) == (129, 1029 * 15000000)
    assert any(
        report["job"] is not None
        and report["source"]["state"] == "prepared"
        and report["source"]["after_seq"] < 2064
        for report in reports
    )
    assert all(
        not report["quota_usage_available"] and report["time_window_coverage"] == "not_assessed"
        for report in reports
    )
    # 129 small measurements exceed the per-proof receipt limit unless only local edges
    # select candidates; the limit itself remains 128.
    # Extend the same real long-run fixture through resumable source coverage; no second replay.
    from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
    from aris.modules.lyra.coverage_service import ProcessingCoverageService
    from tests.test_coverage_builds import build_request, build_service, drain
    from tests.test_usage_ledger import POST

    pipeline = graph.pump.pipelines[0]
    coverage = ProcessingCoverageService(
        state[0].sessions,
        pipeline.attestations,
        pipeline.trace.archive,
        pipeline.plans.acceptances.identities,
        configuration=ProcessingCoverageConfiguration(
            trace=pipeline.configuration.trace, recording=config.recording, gpu_uuids=("GPU-a",)
        ),
        clock=lambda: state[4]["now"],
    )
    builds = build_service(coverage, timeout_ms=300000)
    plan = builds.create(POST, build_request(builds, end=10 + 15 * 1029))
    assert plan.target_revision == 17
    window = drain(builds, plan).snapshot.window
    assert window.source_prefix_complete and window.coverage == "complete"
    assert window.total_usage_microseconds == 1029 * 15000000
