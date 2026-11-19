from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text

from aris.core.contracts import DomainError
from aris.modules.lyra.recording_pipeline_service import RecordingPipelineService
from aris.modules.lyra.recording_plan_repo import RecordingPlanRepository
from aris.modules.lyra.recording_plan_service import RecordingPlanService
from aris.modules.lyra.recording_pump import RecordingPump
from aris.modules.lyra.recording_pump_schemas import RecordingPumpConfiguration
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from tests.test_heartbeat import beat, heartbeat
from tests.test_recording_pipeline import (
    admin_state,
    counts,
    derivations,
    history_state,
    incoming,
    ledger,
    open_batches,
    partition_recording,
    pipeline,
    postgres,
)
from tests.test_usage_ledger import POST

__all__ = [
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "pipeline",
]


def pump(state, *, sources=True):
    service, _ = state
    return RecordingPump(
        (service,) if sources else (),
        service.trace.archive,
        service.plans,
        service.plans.jobs,
        clock=service.clock,
        configuration=RecordingPumpConfiguration(
            recording=service.configuration.recording,
            method=service.configuration.method,
            source_limit=8,
            dispatch_limit=1,
        ),
    )


def recorded(service):
    with service.sessions() as session:
        return session.execute(
            text("SELECT count(*),coalesce(sum(observed_microseconds),0) FROM usage_ledger_entries")
        ).one()


def test_rebuilt_ticks_continue_source_to_plan_and_jobs_without_in_memory_progress(pipeline):
    service, _ = pipeline
    results = [pump(pipeline).tick(POST) for _ in range(5)]
    assert [result.source.after_seq for result in results] == [10, 18, 24, 24, 24]
    assert [result.source.state for result in results] == ["prepared"] * 3 + ["caught_up"] * 2
    assert all(not result.failures and not result.quota_usage_available for result in results)
    assert [result.job.state if result.job else None for result in results] == [
        None,
        None,
        "completed",
        "completed",
        "completed",
    ]
    assert recorded(service) == (3, 135000000)
    before = counts(service)
    final = pump(pipeline).tick(POST)
    assert final.source.state == "caught_up" and final.dispatch is None and final.job is None
    assert counts(service) == before and recorded(service) == (3, 135000000)


def test_public_ceiling_does_not_bridge_a_gap_while_saved_partitions_can_finish(
    pipeline, partition_recording
):
    service, _ = pipeline
    assert beat(partition_recording[2][4], heartbeat(seq=28)).status_code == 200
    results = [pump(pipeline).tick(POST) for _ in range(5)]
    assert results[2].source.state == "prepared" and results[2].source.missing_seq == 25
    assert all(
        result.source.state == "gap" and result.source.missing_seq == 25 for result in results[3:]
    )
    assert all(result.source.ceiling == 28 for result in results)
    assert service.trace.position().after_seq == 24 and recorded(service) == (3, 135000000)
    assert all(not result.quota_usage_available for result in results)


def test_default_pause_has_no_database_or_source_calls(pipeline, monkeypatch):
    service, _ = pipeline
    idle = RecordingPump(
        (service,), service.trace.archive, service.plans, service.plans.jobs, clock=service.clock
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("paused pump accessed a dependency")

    before = counts(service)
    with monkeypatch.context() as patch:
        patch.setattr(service.trace.archive, "head", unexpected)
        patch.setattr(service.plans, "dispatch_next", unexpected)
        patch.setattr(service.plans.jobs, "claim", unexpected)
        result = idle.tick(POST)
    assert result.paused and not result.failures and result.source is result.job is None
    assert counts(service) == before


@pytest.mark.parametrize("stage", ["source", "execute"])
def test_unknown_commit_in_a_stage_recovers_from_committed_database_work(
    pipeline, monkeypatch, stage
):
    service, _ = pipeline
    open_batches(service)
    worker = pump(pipeline)
    if stage == "source":
        import aris.modules.lyra.recording_pipeline_service as module

        expected_call = 1
    else:
        import aris.modules.lyra.recording_job_service as module

        # The first job commit leases it; the second records and completes it.
        expected_call = 2
    commit = module.commit_owned_transaction
    calls = []

    def unknown(session):
        calls.append(True)
        commit(session)
        if len(calls) == expected_call:
            raise RuntimeError("fixture pump commit outcome unknown")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", unknown)
        with pytest.raises(RuntimeError):
            worker.tick(POST)
    assert service.trace.position().after_seq == 24
    for _ in range(3):
        assert not pump(pipeline).tick(POST).failures
    assert recorded(service) == (3, 135000000)
    with service.sessions() as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM usage_pipeline_batches "
                    "WHERE consumer_id='pipeline-fixture'"
                )
            )
            == 3
        )
        assert session.scalar(text("SELECT count(*) FROM usage_recording_plans")) == 1


def test_source_failure_does_not_discard_saved_dispatch_and_measurement_work(pipeline, monkeypatch):
    service, _ = pipeline
    service.consume(POST, incoming(service, limit=128))

    def unavailable(*args):
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture source unavailable", 503)

    monkeypatch.setattr(service.trace.archive, "head", unavailable)
    result = pump(pipeline).tick(POST)
    assert [(value.stage, value.code) for value in result.failures] == [
        ("source", "DEPENDENCY_UNAVAILABLE")
    ]
    assert result.source is None and result.dispatch is not None and result.job.state == "completed"
    assert recorded(service) == (1, 45000000)


def test_pause_after_committed_source_reports_real_progress_and_preserves_the_plan(
    pipeline, monkeypatch
):
    service, _ = pipeline
    open_batches(service)
    worker = pump(pipeline)
    consume = service.consume

    def pause(*args):
        value = consume(*args)
        worker.configuration = None
        return value

    monkeypatch.setattr(service, "consume", pause)
    result = worker.tick(POST)
    assert result.source.state == "prepared" and result.source.after_seq == 24
    assert {value.stage for value in result.failures} == {"source", "dispatch", "execute"}
    assert all(value.code == "USAGE_NOT_CONFIGURED" for value in result.failures)
    assert result.dispatch is result.job is None and recorded(service) == (0, 0)
    for _ in range(3):
        pump(pipeline, sources=False).tick(POST)
    assert recorded(service) == (3, 135000000)


def test_incompatible_plans_and_jobs_remain_pending_without_consuming_attempts(pipeline):
    service, _ = pipeline
    batch = service.consume(POST, incoming(service, limit=128)).batch
    current = service.plans.progress(batch.outcomes[0].plan_id).plan
    ledger = service.plans.ledger
    original = ledger.configuration
    foreign = original.model_copy(update={"source_ref": "another-explicit-recording-context"})
    ledger.configuration = foreign
    try:
        other_config = service.plans.create(
            POST,
            current.request.model_copy(
                update={
                    "id": uuid4(),
                    "expected_recording_configuration_fingerprint": foreign.fingerprint,
                }
            ),
        ).plan
    finally:
        ledger.configuration = original
    old_evidence = service.plans.evidence
    method = ResidencyPartitionMethod(max_intervals=5)
    proof = ResidencyPartitionService(
        service.sessions,
        service.attestations,
        service.trace,
        service.segments,
        service.trace.archive,
        method=method,
        clock=service.clock,
    )
    alternate = RecordingPlanService(
        service.sessions, proof, ledger, service.plans.jobs, clock=service.clock
    )
    other_method = alternate.create(
        POST,
        current.request.model_copy(
            update={
                "id": uuid4(),
                "partition": current.request.partition.model_copy(
                    update={"expected_method_fingerprint": method.fingerprint}
                ),
            }
        ),
    ).plan
    assert service.plans.evidence is old_evidence
    wrong_jobs = [
        current.job(0).model_copy(
            update={
                "id": uuid4(),
                "expected_recording_configuration_fingerprint": foreign.fingerprint,
            }
        ),
        current.job(1).model_copy(
            update={
                "id": uuid4(),
                "partition": current.request.partition.model_copy(
                    update={"expected_method_fingerprint": method.fingerprint}
                ),
            }
        ),
    ]
    for value in wrong_jobs:
        service.plans.jobs.enqueue(POST, value)
    for _ in range(3):
        assert not pump(pipeline, sources=False).tick(POST).failures
    assert recorded(service) == (3, 135000000)
    for plan in (other_config, other_method):
        assert service.plans.progress(plan.request.id).next_partition == 0
    for value in wrong_jobs:
        saved = service.plans.jobs.exact(value.id)
        assert saved.state == "queued" and saved.attempts == 0
    final = pump(pipeline, sources=False).tick(POST)
    assert final.dispatch is final.job is None and not final.failures


def test_round_robin_source_failure_does_not_starve_a_registered_source(pipeline):
    service, _ = pipeline
    config = service.configuration.model_copy(
        update={
            "trace": service.configuration.trace.model_copy(
                update={
                    "consumer_id": "unregistered-pipeline-fixture",
                    "agent_id": "agent-unregistered",
                    "node_id": "node-unregistered",
                }
            )
        }
    )
    trace = UsageTraceConsumerService(
        service.sessions,
        service.trace.archive,
        service.trace.observations,
        configuration=config.trace,
        clock=service.clock,
    )
    unknown = RecordingPipelineService(
        service.sessions,
        trace,
        service.attestations,
        service.segments,
        service.plans,
        configuration=config,
        clock=service.clock,
    )
    worker = pump(pipeline)
    worker.pipelines = (unknown, service)
    first = worker.tick(POST)
    assert first.failures[0].stage == "source" and first.failures[0].code == "NOT_FOUND"
    second = worker.tick(POST)
    assert second.source.consumer_id == "pipeline-fixture" and second.source.after_seq == 10
    third = worker.tick(POST)
    assert third.failures[0].consumer_id == "unregistered-pipeline-fixture"
    fourth = worker.tick(POST)
    assert fourth.source.after_seq == 18


def test_pump_rejects_repeated_sources_and_wrong_handler_before_writing(pipeline):
    service, _ = pipeline
    worker = pump(pipeline)
    before = counts(service)
    worker.pipelines = (service, service)
    with pytest.raises(DomainError) as error:
        worker.tick(POST)
    assert error.value.code == "CONFLICT" and counts(service) == before
    worker.pipelines = (service,)
    service.plans.jobs.partition_recorder = None
    with pytest.raises(DomainError) as error:
        worker.tick(POST)
    assert error.value.code == "CONFLICT" and counts(service) == before


def test_postgresql_pending_plan_selection_skips_a_locked_matching_plan(pipeline):
    postgres(pipeline)
    service, _ = pipeline
    batch = service.consume(POST, incoming(service, limit=128)).batch
    first = service.plans.progress(batch.outcomes[0].plan_id).plan
    second = service.plans.create(POST, first.request.model_copy(update={"id": uuid4()})).plan
    selection = pump(pipeline).configuration.selection
    with service.sessions.begin() as holder:
        held = RecordingPlanRepository(holder).next_pending(selection)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(service.plans.dispatch_next, POST, selection).result(timeout=5)
        assert str(result.progress.plan.request.id) != held.id
    assert sorted(
        service.plans.progress(value.request.id).next_partition for value in (first, second)
    ) == [0, 1]


def test_postgresql_concurrent_ticks_recover_and_record_each_partition_once(pipeline):
    postgres(pipeline)
    service, _ = pipeline
    workers = [pump(pipeline), pump(pipeline)]
    gate = Barrier(2)

    def tick(worker):
        gate.wait(timeout=5)
        return worker.tick(POST)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(tick, workers))
    assert all(
        value.code == "CONFLICT" and value.stage == "source"
        for result in results
        for value in result.failures
    )
    for _ in range(6):
        assert not pump(pipeline).tick(POST).failures
    assert recorded(service) == (3, 135000000) and service.trace.position().after_seq == 24
