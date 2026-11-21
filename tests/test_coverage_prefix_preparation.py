import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from aris.apps.composition.lyra_coverage import (
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.modules.lyra.source_prefix_service import SourcePrefixService
from tests.test_coverage_builds import build_service, counts
from tests.test_lyra_coverage_worker import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    private_config,
    process,
    runtime,
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


def automatic(stable_recording):
    config, coverage = runtime(stable_recording)
    query = config.target.query.model_copy(
        update={"start_at": config.target.query.start_at + timedelta(seconds=60)}
    )
    target = config.target.model_copy(update={"query": query})
    return LyraCoverageRuntimeConfig.model_validate(
        {**config.model_dump(), "target": target.model_dump(), "prepare_prefix": True}
    ), coverage


def test_bounded_prefix_preparation_then_admission_reuses_original_prefix_after_rebuild(
    stable_recording, monkeypatch
):
    config, coverage = automatic(stable_recording)
    build = build_service(coverage)
    states = []
    for _ in range(10):
        graph = build_lyra_coverage_components(coverage.sessions, config, clock=coverage.clock)
        result = graph.run_once()
        states.append(result["state"])
        if result["state"] == "admitted":
            break
    assert states == ["prefix_prepared", "prefix_prepared", "admitted"]
    plan = build.exact(config.target.id)
    assert plan.request.prefix_checkpoint.through_revision == 1 and plan.start_revision == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("admitted target reselected its prefix")

    monkeypatch.setattr(SourcePrefixService, "status", forbidden)
    for _ in range(10):
        graph = build_lyra_coverage_components(coverage.sessions, config, clock=coverage.clock)
        result = graph.run_once()
        if result["state"] == "finished":
            break
    assert result["state"] == "finished"
    snapshot = build.windows.exact(plan.window_id)
    assert snapshot.window.prefix_checkpoint == plan.request.prefix_checkpoint
    assert snapshot.window.coverage == "complete"
    assert snapshot.window.total_usage_microseconds == 75000000
    assert build.windows.reverify(plan.window_id) == snapshot
    assert build.exact(config.target.id) == plan


def test_auto_status_is_read_only_and_manual_prefix_cannot_also_be_selected(stable_recording):
    config, coverage = automatic(stable_recording)
    graph = build_lyra_coverage_components(coverage.sessions, config, clock=coverage.clock)
    before = counts(build_service(coverage))
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    engine = coverage.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        report = graph.run_once(status_only=True)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert report["state"] == "not_started" and report["prefix"]["latest"] is None
    assert not statements and counts(build_service(coverage)) == before
    assert graph.run_once()["state"] == "prefix_prepared"
    prefix = graph.prefixes.service.status(
        config.configuration.trace, config.target.query.start_at
    ).eligible
    assert prefix is not None
    with pytest.raises(ValidationError, match="exclusive"):
        LyraCoverageRuntimeConfig.model_validate(
            {
                **config.model_dump(),
                "target": {**config.target.model_dump(), "prefix_checkpoint": prefix.model_dump()},
            }
        )
    paused = config.model_copy(update={"enabled": False})
    graph = build_lyra_coverage_components(coverage.sessions, paused, clock=coverage.clock)
    assert (
        graph.service is None and graph.prefixes is None and graph.run_once()["state"] == "paused"
    )


def test_real_cli_once_resumes_prefix_and_finite_build_with_one_config(stable_recording, tmp_path):
    config, coverage = automatic(stable_recording)
    config = config.model_copy(update={"target": config.target.model_copy(update={"id": uuid4()})})
    path = private_config(tmp_path / "auto-coverage.json", config)
    lag = (coverage.clock() - datetime.now(UTC)).total_seconds()
    assert lag < 2
    if lag > 0:
        time.sleep(lag + 0.02)
    states = []
    for _ in range(12):
        child = process(path, coverage, "--once")
        assert child.returncode == 0, child.stdout
        report = json.loads(child.stdout)
        states.append(report["state"])
        if report["state"] == "finished":
            break
    assert states[:3] == ["prefix_prepared", "prefix_prepared", "admitted"]
    assert states[-1] == "finished" and report["coverage"] == "complete"
    saved = build_service(coverage).exact(config.target.id)
    assert saved.request.prefix_checkpoint.through_revision == 1
