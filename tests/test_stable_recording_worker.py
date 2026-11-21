import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aris.apps.composition.lyra import LyraRuntimeConfig, LyraSourceBinding, build_lyra_components
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.stable_pump import StableRecordingPump
from aris.modules.lyra.stable_pump_schemas import StableRecordingPumpConfiguration
from tests.test_lyra_composition import configuration, private_file, process
from tests.test_mapping_history import assign
from tests.test_recording_pump import recorded
from tests.test_stable_pipeline import (
    admin_state,
    derivations,
    history_state,
    incoming,
    ledger,
    partition_recording,
    sizes,
    stable_pipeline,
)
from tests.test_usage_ledger import POST

__all__ = [
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_pipeline",
]


def stable_config(state):
    return LyraRuntimeConfig.model_validate(
        {
            **configuration(state).model_dump(),
            "method": StableResidencyLayoutMethod(max_intervals=3),
        }
    )


def pump(pipeline):
    return StableRecordingPump(
        (pipeline,),
        pipeline.trace.archive,
        pipeline.plans,
        pipeline.plans.jobs,
        clock=pipeline.clock,
        configuration=StableRecordingPumpConfiguration(
            recording=pipeline.configuration.recording,
            method=pipeline.configuration.method,
            source_limit=8,
            dispatch_limit=1,
        ),
    )


def test_private_stable_graph_records_a_live_prefix_then_resumes_closure_without_legacy_work(
    partition_recording,
):
    state = partition_recording
    config = stable_config(state)
    graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    old_job = graph.pump.jobs.enqueue(POST, state[1]).job
    reports = []
    for index in range(6):
        graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
        reports.append(graph.run_once())
        if index == 1:
            assert recorded(state[0]) == (1, 45000000)
            assert (
                graph.pump.plans.progress(
                    reports[-1]["dispatch"]["layout_key"]
                ).witness.state_at_record
                == "open"
            )
    assert [item["source"]["after_seq"] for item in reports] == [10, 18, 24, 24, 24, 24]
    assert all(not item["failures"] and item["mode"] == "lyra-recording-stable" for item in reports)
    assert all(
        not item["quota_usage_available"] and item["time_window_coverage"] == "not_assessed"
        for item in reports
    )
    assert recorded(state[0]) == (3, 135000000)
    assert graph.pump.jobs.exact(old_job.request.id).attempts == 0
    assert graph.run_once()["dispatch"]["state"] == "idle"


def test_current_identity_refresh_and_cutoff_race_rollback_keep_historical_batches(
    stable_pipeline, monkeypatch
):
    pipeline, _, state, clock = stable_pipeline
    head = state[0].identities
    pipeline.identity_head = head
    initial = pipeline.refresh_identity_configuration()
    batch = pipeline.consume(POST, incoming(pipeline)).batch
    history = state[2][2][2]
    present = clock["now"]
    change = assign(history, 80, 1, expected=history[4][0])
    clock["now"] = present
    refreshed = pipeline.refresh_identity_configuration()
    assert refreshed.identity_revision == change.revision > initial.identity_revision
    assert pipeline.exact(batch.request.source.id) == batch
    request, before, position = incoming(pipeline), sizes(pipeline), pipeline.trace.position()
    original = head.revision_in_transaction
    with monkeypatch.context() as patch:
        patch.setattr(head, "revision_in_transaction", lambda db: original(db) + 1)
        with pytest.raises(DomainError) as error:
            pipeline.consume(POST, request)
        assert error.value.code == "IDENTITY_SNAPSHOT_CHANGED"
    assert sizes(pipeline) == before and pipeline.trace.position() == position
    newer = pipeline.consume(POST, request).batch
    assert newer.configuration.identity_revision == change.revision
    assert (
        pipeline.exact(batch.request.source.id).configuration.identity_revision
        == initial.identity_revision
    )
    pipeline.identity_head = None
    with pytest.raises(DomainError) as error:
        pump(pipeline).tick(POST)
    assert error.value.code == "CONFLICT"


def test_source_page_backpressure_is_relearned_without_losing_durable_cursor(
    stable_pipeline, monkeypatch
):
    pipeline, _, state, _ = stable_pipeline
    pipeline.identity_head = state[0].identities
    service = pump(pipeline)
    requests, original = [], pipeline.consume

    def bounded(context, request):
        requests.append(request)
        if request.source.limit > 4:
            raise DomainError(
                "PIPELINE_BATCH_TOO_LARGE", "fixture source preparation backpressure", 503
            )
        return original(context, request)

    monkeypatch.setattr(pipeline, "consume", bounded)
    first = service.tick(POST)
    assert (
        first.failures[0].code == "PIPELINE_BATCH_TOO_LARGE"
        and pipeline.trace.position().after_seq == 2
    )
    second = service.tick(POST)
    assert not second.failures and second.source.after_seq == 6
    assert requests[0].source.id != requests[1].source.id and requests[1].source.limit == 4
    rebuilt = pump(pipeline)
    assert rebuilt.tick(POST).failures[0].code == "PIPELINE_BATCH_TOO_LARGE"
    assert pipeline.trace.position().after_seq == 6
    assert rebuilt.tick(POST).source.after_seq == 10


def test_source_failure_does_not_cancel_ready_dispatch_and_measurement(partition_recording):
    state = partition_recording
    config = stable_config(state)
    config = config.model_copy(
        update={
            "sources": (
                LyraSourceBinding(
                    consumer_id="unknown-stable-source",
                    agent_id="agent-unknown",
                    node_id="node-unknown",
                    initial_after_seq=0,
                ),
                *config.sources,
            )
        }
    )
    graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    healthy = graph.pump.pipelines[1]
    healthy.refresh_identity_configuration()
    healthy.consume(POST, incoming(healthy))
    healthy.consume(POST, incoming(healthy))
    report = graph.run_once()
    assert report["failures"][0]["stage"] == "source"
    assert report["dispatch"]["state"] == "enqueued" and report["job"]["state"] == "completed"
    assert recorded(state[0]) == (1, 45000000)
    assert graph.run_once()["source"]["consumer_id"] == config.sources[1].consumer_id


def test_stable_private_configuration_and_real_once_processes_resume_existing_work(
    partition_recording, tmp_path
):
    state, config = partition_recording, stable_config(partition_recording)
    with pytest.raises(ValidationError, match="final-tail"):
        LyraRuntimeConfig.model_validate(
            {**config.model_dump(), "method": StableResidencyLayoutMethod(max_intervals=2)}
        )
    disabled = build_lyra_components(state[0].sessions, LyraRuntimeConfig(method=config.method))
    assert disabled.run_once()["paused"] and disabled.run_once()["mode"] == "lyra-recording-stable"
    path = private_file(tmp_path, config)
    reports = []
    for _ in range(2):
        result = process(path, state)
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(json.loads(result.stdout))
        assert "ARIS_DATABASE_URL" not in result.stdout
    assert [report["source"]["after_seq"] for report in reports] == [10, 18]
    assert reports[-1]["job"]["state"] == "completed" and recorded(state[0]) == (1, 45000000)
    state[4]["now"] = max(datetime.now(UTC), state[4]["now"])
    graph = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    for _ in range(3):
        assert not graph.run_once()["failures"]
    assert recorded(state[0]) == (3, 135000000)
