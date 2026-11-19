from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import (
    UsageRecordingPipelineBatchRow,
    UsageRecordingPlanRow,
    UsageResidencySegmentSetRow,
    UsageTraceAttestationRow,
    UsageTraceRecordRow,
)
from aris.modules.lyra.partition_recording_service import PartitionRecordingService
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.recording_pipeline_schemas import (
    RecordingPipelineConfiguration,
    RecordingPipelineLimits,
    RecordingPipelineRequest,
)
from aris.modules.lyra.recording_pipeline_service import RecordingPipelineService
from aris.modules.lyra.recording_plan_schemas import RecordingDispatchRequest
from aris.modules.lyra.recording_plan_service import RecordingPlanService
from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
from aris.modules.lyra.residency_segment_service import ResidencySegmentService
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_schemas import TraceConsumptionIdle, UsageTraceConsumeRequest
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from tests.test_administration import NOW
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_partition_recording import (
    METHOD,
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
)
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


def build(state):
    old, original, ledger, whole, clock = state

    def now():
        return clock["now"]

    trace_config = old.evidence.records.configuration.model_copy(
        update={"consumer_id": "pipeline-fixture"}
    )
    archive = old.evidence.archive
    trace = UsageTraceConsumerService(
        old.sessions, archive, old.derivations.observations, configuration=trace_config, clock=now
    )
    attestations = TraceAttestationService(old.sessions, trace, clock=now)
    segments = ResidencySegmentStore(
        old.sessions,
        ResidencySegmentService(
            old.sessions, attestations, archive, old.derivations.observations, clock=now
        ),
        clock=now,
    )
    proof = ResidencyPartitionService(
        old.sessions, attestations, trace, segments, archive, method=METHOD, clock=now
    )
    writer = PartitionRecordingService(
        old.sessions, proof, old.identities, old.derivations, old.ledger, clock=now
    )
    jobs = RecordingJobService(
        old.sessions,
        whole,
        partition_recorder=writer,
        limits=RecordingJobLimits(
            transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
        ),
        clock=now,
    )
    plans = RecordingPlanService(old.sessions, proof, old.ledger, jobs, clock=now)
    config = RecordingPipelineConfiguration(
        source_ref="explicit-isolated-pipeline-fixture",
        trace=trace_config,
        recording=old.ledger.configuration,
        method=METHOD,
        identity_revision=original.identity_revision,
    )
    pipeline = RecordingPipelineService(
        old.sessions, trace, attestations, segments, plans, configuration=config, clock=now
    )
    return pipeline, clock


@pytest.fixture
def pipeline(partition_recording):
    return build(partition_recording)


def incoming(service, *, limit=8):
    return RecordingPipelineRequest(
        source=UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=service.configuration.trace.fingerprint,
            expected_revision=service.trace.position().revision,
            through_seq=24,
            limit=limit,
        ),
        expected_configuration_fingerprint=service.configuration.fingerprint,
    )


def counts(service):
    with service.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                UsageRecordingPipelineBatchRow,
                UsageRecordingPlanRow,
                UsageTraceRecordRow,
                UsageTraceAttestationRow,
                UsageResidencySegmentSetRow,
                DomainEvent,
                OutboxEntry,
            )
        )


def open_batches(service):
    receipts = [service.consume(POST, incoming(service)) for _ in range(2)]
    assert all(not value.batch.outcomes for value in receipts)
    assert service.trace.position().after_seq == 18
    return receipts


def postgres(pipeline):
    if pipeline[0].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL atomic source pipeline")


def test_open_to_closed_preparation_retains_every_stage_and_then_executes_fixed_jobs(pipeline):
    service, _ = pipeline
    before = counts(service)
    receipts = open_batches(service)
    last = service.consume(POST, incoming(service))
    batch = last.batch
    assert not last.duplicate and len(batch.outcomes) == 1 and batch.outcomes[0].state == "planned"
    assert batch.source_count == 6 and batch.first_seq == 19 and batch.last_seq == 24
    assert counts(service)[:5] == tuple(
        a + b for a, b in zip(before[:5], (3, 1, 3, 3, 3), strict=True)
    )
    assert service.trace.position().after_seq == 24
    outcome = batch.outcomes[0]
    plan = service.plans.progress(outcome.plan_id).plan
    assert plan.partition_count == 3 and service.plans.progress(plan.request.id).next_partition == 0
    assert batch.time_window_coverage == "not_assessed" and not batch.quota_usage_available
    with service.sessions() as session:
        assert session.scalar(text("SELECT count(*) FROM usage_recording_jobs")) == 0
    service.plans.dispatch(
        POST, RecordingDispatchRequest(plan_id=plan.request.id, expected_version=1, limit=3)
    )
    for _ in range(3):
        claim = service.plans.jobs.claim(POST)
        assert service.plans.jobs.execute(POST, claim).state == "completed"
    with service.sessions() as session:
        assert (
            session.scalar(text("SELECT sum(observed_microseconds) FROM usage_ledger_entries"))
            == 135000000
        )
    assert service.exact(batch.request.source.id) == batch
    assert all(service.exact(value.batch.request.source.id) == value.batch for value in receipts)


def test_replay_keeps_the_original_cutoff_and_reads_while_paused(pipeline):
    service, clock = pipeline
    request = incoming(service)
    first = service.consume(POST, request)
    before = counts(service)
    clock["now"] += timedelta(days=1)
    service.configuration = None
    replay = service.consume(POST, request)
    assert replay.duplicate and replay.batch == first.batch and counts(service) == before
    assert service.exact(request.source.id) == first.batch
    with pytest.raises(DomainError) as error:
        service.consume(
            POST, request.model_copy(update={"expected_configuration_fingerprint": "f" * 64})
        )
    assert error.value.code == "CONFLICT"
    with pytest.raises(DomainError) as error:
        service.consume(
            POST,
            request.model_copy(
                update={"source": request.source.model_copy(update={"id": uuid4()})}
            ),
        )
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(service) == before


@pytest.mark.parametrize(
    "table",
    [
        "usage_trace_records",
        "usage_trace_attestations",
        "usage_residency_segment_sets",
        "usage_recording_plans",
        "usage_pipeline_batches",
        "event_outbox",
    ],
)
def test_failure_at_each_stage_rolls_back_cursor_sources_plans_and_batch(pipeline, table):
    service, _ = pipeline
    open_batches(service)
    request = incoming(service)
    before = counts(service)
    position = service.trace.position()
    engine = service.sessions.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table + " "):
            raise RuntimeError("fixture pipeline stage interruption")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            service.consume(POST, request)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(service) == before and service.trace.position() == position
    assert service.consume(POST, request).batch.outcomes[0].state == "planned"


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_pipeline_commit_recovers_all_original_bindings(pipeline, monkeypatch, when):
    import aris.modules.lyra.recording_pipeline_service as module

    service, _ = pipeline
    open_batches(service)
    request = incoming(service)
    before = counts(service)
    commit = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            commit(session)
        raise RuntimeError("fixture pipeline commit outcome unknown")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError):
            service.consume(POST, request)
    recovered = service.consume(POST, request)
    assert recovered.duplicate == (when == "after")
    assert counts(service)[:5] == tuple(
        a + b for a, b in zip(before[:5], (1, 1, 1, 1, 1), strict=True)
    )
    assert service.trace.position().revision == 3 and service.trace.position().after_seq == 24


@pytest.mark.parametrize("change", ["pipeline", "clock"])
def test_late_pause_or_clock_change_rolls_back_the_entire_prepared_batch(
    pipeline, monkeypatch, change
):
    service, clock = pipeline
    open_batches(service)
    request = incoming(service)
    before = counts(service)
    emit = service._emit

    def changed(*args):
        emit(*args)
        if change == "pipeline":
            service.configuration = None
        else:
            clock["now"] -= timedelta(seconds=1)

    monkeypatch.setattr(service, "_emit", changed)
    with pytest.raises(DomainError):
        service.consume(POST, request)
    assert counts(service) == before and service.trace.position().after_seq == 18


def test_explicit_review_is_retained_but_storage_failure_cannot_be_relabelled_review(
    pipeline, monkeypatch
):
    service, _ = pipeline
    open_batches(service)
    request = incoming(service)
    before = counts(service)

    def unavailable(*args):
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture missing index", 503)

    with monkeypatch.context() as patch:
        patch.setattr(service.plans, "create_in_transaction", unavailable)
        with pytest.raises(DomainError):
            service.consume(POST, request)
    assert counts(service) == before and service.trace.position().after_seq == 18

    def review(*args):
        raise DomainError("USAGE_NEEDS_REVIEW", "fixture reviewed boundary", 409)

    monkeypatch.setattr(service.plans, "create_in_transaction", review)
    result = service.consume(POST, request)
    assert result.batch.outcomes[0].state == "needs_review"
    assert result.batch.outcomes[0].plan_id is None and not result.batch.quota_usage_available
    assert counts(service)[1] == before[1] and service.trace.position().after_seq == 24


@pytest.mark.parametrize("boundary", ["short", "recording_start"])
def test_real_short_run_and_recording_start_review_are_not_reported_as_zero_usage(
    pipeline, boundary
):
    service, _ = pipeline
    trace = service.configuration.trace.model_copy(
        update={
            "formation": service.configuration.trace.formation.model_copy(
                update={"minimum_intervals": 10 if boundary == "short" else 2}
            )
        }
    )
    recording = service.configuration.recording.model_copy(
        update={
            "timing_fingerprint": residency_fingerprint(trace.continuity, trace.formation),
            "recording_start_at": NOW if boundary == "short" else NOW + timedelta(seconds=60),
        }
    )
    service.trace.configuration = trace
    service.plans.ledger.configuration = recording
    service.configuration = service.configuration.model_copy(
        update={"trace": trace, "recording": recording}
    )
    result = service.consume(POST, incoming(service, limit=128))
    assert result.batch.outcomes[0].state == (
        "not_qualified" if boundary == "short" else "needs_review"
    )
    assert result.batch.outcomes[0].code == (
        "RESIDENCY_NOT_STABLE" if boundary == "short" else "USAGE_NEEDS_REVIEW"
    )
    assert result.batch.outcomes[0].plan_id is None and not result.batch.quota_usage_available
    assert service.exact(result.batch.request.source.id) == result.batch


def test_plan_batch_limit_rolls_back_every_closed_run_instead_of_truncating_the_list(
    pipeline, partition_recording
):
    service, _ = pipeline
    client = partition_recording[2][4]
    for index in range(3):
        payload = ownership(
            resource_batch(
                seq=25 + 2 * index, observed_at=NOW + timedelta(seconds=175 + 15 * index)
            ),
            [owner(username="linux-user")],
        )
        assert (
            put(client, payload).status_code
            == beat(client, heartbeat(seq=26 + 2 * index)).status_code
            == 200
        )
    idle = resource_batch(seq=31, observed_at=NOW + timedelta(seconds=220))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(client, idle).status_code == beat(client, heartbeat(seq=32)).status_code == 200
    original = incoming(service, limit=128)
    request = original.model_copy(
        update={"source": original.source.model_copy(update={"through_seq": 32})}
    )
    before = counts(service)
    service.limits = RecordingPipelineLimits(max_plans_per_batch=1)
    with pytest.raises(DomainError) as error:
        service.consume(POST, request)
    assert error.value.code == "PIPELINE_BATCH_TOO_LARGE" and counts(service) == before
    assert service.trace.position().revision == 0 and service.trace.position().after_seq == 2
    service.limits = RecordingPipelineLimits(max_plans_per_batch=2)
    saved = service.consume(POST, request)
    assert len(saved.batch.outcomes) == 2 and all(
        value.state == "planned" for value in saved.batch.outcomes
    )
    assert service.trace.position().after_seq == 32


def test_unfilled_source_gap_cannot_create_a_successful_preparation_or_advance_the_cursor(pipeline):
    service, _ = pipeline
    original = incoming(service, limit=128)
    request = original.model_copy(
        update={"source": original.source.model_copy(update={"through_seq": 28})}
    )
    first = service.consume(POST, request)
    assert first.batch.last_seq == 24
    before = counts(service)
    request = request.model_copy(
        update={"source": request.source.model_copy(update={"id": uuid4(), "expected_revision": 1})}
    )
    idle = service.consume(POST, request)
    assert isinstance(idle, TraceConsumptionIdle) and idle.state == "gap" and idle.missing_seq == 25
    assert counts(service) == before and service.trace.position().after_seq == 24


def test_caught_up_does_not_create_a_fictitious_pipeline_batch_and_outer_owner_can_rollback(
    pipeline,
):
    service, _ = pipeline
    request = incoming(service, limit=128)
    before = counts(service)
    with service.sessions() as session:
        service.consume_in_transaction(session, POST, request)
        session.rollback()
    assert counts(service) == before and service.trace.position().revision == 0
    service.consume(POST, request)
    before = counts(service)
    idle = service.consume(POST, incoming(service))
    assert isinstance(idle, TraceConsumptionIdle) and idle.state == "caught_up"
    assert counts(service) == before and service.trace.position().after_seq == 24


def test_pipeline_batch_history_and_nonempty_downgrade_are_protected(pipeline):
    service, _ = pipeline
    first = service.consume(POST, incoming(service))
    for statement in (
        delete(UsageRecordingPipelineBatchRow),
        update(UsageRecordingPipelineBatchRow).values(canonical_batch="{}"),
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as session:
            session.execute(statement)
    config = Config("alembic.ini")
    with service.sessions.kw["bind"].connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="pipeline batch history prevents downgrade"):
            command.downgrade(config, "0023")
    assert service.exact(first.batch.request.source.id) == first.batch


def test_postgresql_same_request_and_cursor_competition_commit_only_one_batch(pipeline):
    postgres(pipeline)
    service, _ = pipeline
    request = incoming(service)
    gate = Barrier(2)

    def same(_):
        gate.wait(timeout=5)
        return service.consume(POST, request).duplicate

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(same, range(2)))
    assert sorted(values) == [False, True]
    request = incoming(service)
    gate = Barrier(2)

    def competing(_):
        gate.wait(timeout=5)
        try:
            return service.consume(
                POST,
                request.model_copy(
                    update={"source": request.source.model_copy(update={"id": uuid4()})}
                ),
            ).batch.revision
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(competing, range(2)))
    assert set(values) == {2, "CONFLICT"} and service.trace.position().revision == 2


def test_postgresql_pipeline_batch_cannot_be_truncated(pipeline):
    postgres(pipeline)
    service, _ = pipeline
    first = service.consume(POST, incoming(service))
    with pytest.raises(DBAPIError), service.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_pipeline_batches"))
    assert service.exact(first.batch.request.source.id) == first.batch
