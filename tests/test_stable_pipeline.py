from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import (
    StableWindowPlanRow,
    UsageRecordingJobRow,
    UsageRecordingPipelineBatchRow,
    UsageResidencySegmentSetRow,
    UsageTraceAttestationRow,
    UsageTraceRecordRow,
)
from aris.modules.lyra.recording_pipeline_repo import RecordingPipelineRepository
from aris.modules.lyra.recording_pipeline_schemas import RecordingPipelineRequest
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.residency_layout_verification_service import StableResidencyWindowService
from aris.modules.lyra.stable_pipeline_schemas import StableRecordingPipelineConfiguration
from aris.modules.lyra.stable_pipeline_service import StableRecordingPipelineService
from aris.modules.lyra.stable_window_acceptance_service import StableWindowAcceptanceService
from aris.modules.lyra.stable_window_plan_schemas import StableWindowDispatchRequest
from aris.modules.lyra.stable_window_plan_service import StableWindowPlanService
from aris.modules.lyra.stable_window_recording_service import StableWindowRecordingService
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeRequest
from tests.test_administration import NOW
from tests.test_recording_pipeline import (
    admin_state,
    build,
    derivations,
    history_state,
    ledger,
    partition_recording,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


@pytest.fixture
def stable_pipeline(partition_recording):
    closed, clock = build(partition_recording)
    writer = partition_recording[0]
    method = StableResidencyLayoutMethod(max_intervals=3)
    evidence = StableResidencyWindowService(
        closed.sessions,
        closed.plans.evidence,
        method=method,
        clock=closed.clock,
    )
    accepts = StableWindowAcceptanceService(
        closed.sessions,
        evidence,
        writer.identities,
        configuration=writer.ledger.configuration,
        clock=closed.clock,
    )
    stable = StableWindowRecordingService(
        closed.sessions, accepts, writer.derivations, writer.ledger
    )
    jobs = closed.plans.jobs
    jobs.stable_recorder = stable
    plans = StableWindowPlanService(
        closed.sessions,
        evidence,
        accepts,
        jobs,
        configuration=writer.ledger.configuration,
        clock=closed.clock,
    )
    config = StableRecordingPipelineConfiguration(
        source_ref="explicit-stable-pipeline-fixture",
        trace=closed.trace.configuration,
        recording=writer.ledger.configuration,
        method=method,
        identity_revision=closed.configuration.identity_revision,
    )
    pipeline = StableRecordingPipelineService(
        closed.sessions,
        closed.trace,
        closed.attestations,
        closed.segments,
        plans,
        configuration=config,
        clock=closed.clock,
    )
    return pipeline, closed, partition_recording, clock


def incoming(pipeline, *, limit=8):
    return RecordingPipelineRequest(
        source=UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=pipeline.configuration.trace.fingerprint,
            expected_revision=pipeline.trace.position().revision,
            through_seq=24,
            limit=limit,
        ),
        expected_configuration_fingerprint=pipeline.configuration.fingerprint,
    )


def sizes(pipeline):
    with pipeline.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                StableWindowPlanRow,
                UsageRecordingPipelineBatchRow,
                UsageTraceRecordRow,
                UsageTraceAttestationRow,
                UsageResidencySegmentSetRow,
                UsageRecordingJobRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
            )
        )


def test_open_growth_closure_and_historical_batches_share_one_layout_without_closed_plans(
    stable_pipeline,
):
    pipeline, closed, _, _ = stable_pipeline
    initial = pipeline.consume(POST, incoming(pipeline, limit=1)).batch
    assert initial.outcomes == ()  # One isolated sample has not formed a residency interval.
    snapshots = [initial]
    while pipeline.trace.position().after_seq < 24:
        request = incoming(pipeline)
        receipt = pipeline.consume(POST, request)
        assert not receipt.duplicate
        snapshots.append(receipt.batch)
    tracked = [value for batch in snapshots for value in batch.outcomes if value.state == "tracked"]
    assert tracked and len({value.layout_key for value in tracked}) == 1
    key = tracked[0].layout_key
    progress = pipeline.plans.progress(key)
    assert progress.witness.state_at_record == "closed" and progress.next_window == 0
    dispatch = pipeline.plans.dispatch(
        POST,
        StableWindowDispatchRequest(
            key=key,
            expected_version=progress.version,
            limit=8,
        ),
    )
    assert len(dispatch.job_ids) == 3
    while (claim := pipeline.plans.jobs.claim(POST)) is not None:
        assert pipeline.plans.jobs.execute(POST, claim).state == "completed"
    assert all(pipeline.exact(batch.request.source.id) == batch for batch in snapshots)
    pipeline.configuration = None
    assert pipeline.consume(POST, snapshots[0].request).duplicate
    assert pipeline.exact(initial.request.source.id) == initial
    with pytest.raises(DomainError) as error:
        closed.exact(initial.request.source.id)
    assert error.value.code == "CONFLICT"


def test_parent_before_recording_start_keeps_explicit_review_disposition_and_raw_progress(
    stable_pipeline,
):
    pipeline, _, _, _ = stable_pipeline
    recording = pipeline.configuration.recording.model_copy(
        update={"recording_start_at": NOW + timedelta(seconds=20)}
    )
    pipeline.configuration = pipeline.configuration.model_copy(update={"recording": recording})
    pipeline.plans.configuration = pipeline.plans.acceptances.configuration = recording
    pipeline.plans.jobs.stable_recorder.ledger.configuration = recording
    batch = pipeline.consume(POST, incoming(pipeline)).batch
    assert len(batch.outcomes) == 1 and batch.outcomes[0].state == "needs_review"
    assert batch.outcomes[0].code == "USAGE_NEEDS_REVIEW" and batch.outcomes[0].layout_key is None
    assert pipeline.trace.position().after_seq == 10
    assert sizes(pipeline)[0] == 0 and not batch.quota_usage_available
    assert pipeline.exact(batch.request.source.id) == batch


@pytest.mark.parametrize("point", ["attestation", "segments", "plan", "batch", "configuration"])
def test_any_preparation_failure_rolls_back_cursor_and_all_layout_observations(
    stable_pipeline, monkeypatch, point
):
    pipeline, _, _, _ = stable_pipeline
    request, before, position = incoming(pipeline), sizes(pipeline), pipeline.trace.position()
    owner, name = {
        "attestation": (pipeline.attestations, "record_in_transaction"),
        "segments": (pipeline.segments, "record_in_transaction"),
        "plan": (pipeline.plans, "observe_in_transaction"),
        "batch": (RecordingPipelineRepository, "append"),
        "configuration": (pipeline, "_emit"),
    }[point]
    original = getattr(owner, name)

    def fail(*args):
        result = original(*args)
        if point == "configuration":
            pipeline.configuration = None
            return result
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture preparation failed after write", 503)

    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(DomainError):
        pipeline.consume(POST, request)
    assert sizes(pipeline) == before and pipeline.trace.position() == position


@pytest.mark.parametrize("when", ["before", "after"])
def test_commit_acknowledgement_loss_replays_original_atomic_source_batch(
    stable_pipeline, monkeypatch, when
):
    import aris.modules.lyra.stable_pipeline_service as module

    pipeline, _, _, _ = stable_pipeline
    request, before = incoming(pipeline), sizes(pipeline)
    commit = module.commit_owned_transaction

    def interrupted(db):
        if when == "after":
            commit(db)
        raise RuntimeError("fixture lost source commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError, match="lost source commit"):
            pipeline.consume(POST, request)
    result = pipeline.consume(POST, request)
    assert result.duplicate is (when == "after")
    assert sizes(pipeline)[:5] == tuple(value + 1 for value in before[:5])
    assert pipeline.trace.position().revision == 1


def test_postgresql_competing_source_operations_cannot_advance_one_revision_twice(
    stable_pipeline, database_url
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real source transaction locking")
    pipeline, _, _, _ = stable_pipeline
    requests = [incoming(pipeline), incoming(pipeline)]
    before = sizes(pipeline)

    def attempt(request):
        try:
            return pipeline.consume(POST, request)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, requests))
    assert sum(value == "CONFLICT" for value in outcomes) == 1
    assert sizes(pipeline)[:5] == tuple(value + 1 for value in before[:5])
    assert pipeline.trace.position().revision == 1
