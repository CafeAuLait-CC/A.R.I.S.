from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.modules.lyra.models import StableDispatchStateRow
from aris.modules.lyra.recording_job_schemas import RecordingWorkSelection
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.stable_dispatch_service import StableWindowDispatcher
from aris.modules.lyra.stable_pipeline_service import StableRecordingPipelineService
from aris.modules.lyra.stable_window_plan_repo import StableWindowPlanRepository
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from tests.test_stable_pipeline import (
    admin_state,
    derivations,
    history_state,
    incoming,
    ledger,
    partition_recording,
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


def prepare(pipeline):
    batches = []
    while pipeline.trace.position().after_seq < 18:
        batches.append(pipeline.consume(POST, incoming(pipeline)).batch)
    return pipeline.plans.progress(batches[-1].outcomes[0].layout_key)


def second_source(pipeline):
    config = pipeline.configuration
    trace_config = config.trace.model_copy(
        update={"consumer_id": config.trace.consumer_id + "-other"}
    )
    trace = UsageTraceConsumerService(
        pipeline.sessions,
        pipeline.trace.archive,
        pipeline.trace.observations,
        configuration=trace_config,
        clock=pipeline.clock,
    )
    return StableRecordingPipelineService(
        pipeline.sessions,
        trace,
        pipeline.attestations,
        pipeline.segments,
        pipeline.plans,
        configuration=config.model_copy(update={"trace": trace_config}),
        clock=pipeline.clock,
    )


@pytest.fixture
def stable_dispatch(stable_pipeline):
    pipeline, _, _, clock = stable_pipeline
    progress = prepare(pipeline)
    selection = RecordingWorkSelection(
        recording_configuration_fingerprint=pipeline.configuration.recording.fingerprint,
        partition_method_fingerprint=pipeline.configuration.method.fingerprint,
        stable_method_fingerprint=pipeline.configuration.method.fingerprint,
        include_closed_runs=False,
    )
    return StableWindowDispatcher(pipeline.plans), selection, pipeline, progress, clock


def test_blocked_plan_does_not_starve_another_source_and_new_observation_can_retry(
    stable_dispatch, monkeypatch
):
    dispatcher, selection, pipeline, first, _ = stable_dispatch
    other = second_source(pipeline)
    second = prepare(other)
    blocked_key = min(first.key, second.key)
    healthy_key = max(first.key, second.key)
    original = pipeline.plans.dispatch_in_transaction

    def needs_review(session, context, request):
        if request.key == blocked_key:
            raise DomainError("USAGE_NEEDS_REVIEW", "fixture unresolved parent", 409)
        return original(session, context, request)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline.plans, "dispatch_in_transaction", needs_review)
        blocked = dispatcher.dispatch_next(POST, selection)
        assert blocked.state == "blocked" and blocked.attempt.key == blocked_key
        healthy = dispatcher.dispatch_next(POST, selection)
        assert healthy.state == "enqueued" and healthy.dispatch.plan.key == healthy_key
        assert dispatcher.dispatch_next(POST, selection).state == "idle"
    assert pipeline.plans.progress(blocked_key).next_window == 0
    source = pipeline if first.key == blocked_key else other
    source.consume(POST, incoming(source))
    retried = dispatcher.dispatch_next(POST, selection)
    assert retried.state == "enqueued" and retried.dispatch.plan.key == blocked_key
    assert retried.attempt.failures == 0 and not retried.attempt.blocked


def test_backoff_and_failure_limit_survive_service_rebuild_without_advancing_layout(
    stable_dispatch, monkeypatch
):
    dispatcher, selection, pipeline, progress, clock = stable_dispatch

    def unavailable(*args):
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture raw source unavailable", 503)

    monkeypatch.setattr(pipeline.plans, "dispatch_in_transaction", unavailable)
    first = dispatcher.dispatch_next(POST, selection)
    assert first.state == "retry" and first.attempt.failures == 1
    assert dispatcher.dispatch_next(POST, selection).state == "idle"
    clock["now"] += timedelta(seconds=2)
    rebuilt = StableWindowDispatcher(pipeline.plans)
    assert rebuilt.status(progress.key) == first.attempt
    second = rebuilt.dispatch_next(POST, selection)
    assert second.state == "retry" and second.attempt.failures == 2
    clock["now"] += timedelta(seconds=3)
    final = rebuilt.dispatch_next(POST, selection)
    assert final.state == "blocked" and final.attempt.failures == 3
    assert rebuilt.dispatch_next(POST, selection).state == "idle"
    assert pipeline.plans.progress(progress.key) == progress


def test_pause_and_other_cohort_never_consume_a_preparation_attempt(stable_dispatch):
    dispatcher, selection, pipeline, progress, _ = stable_dispatch
    jobs = pipeline.plans.jobs
    handler, jobs.stable_recorder = jobs.stable_recorder, None
    assert dispatcher.dispatch_next(POST, selection).state == "paused"
    assert dispatcher.status(progress.key) is None
    jobs.stable_recorder = handler
    with pytest.raises(DomainError) as error:
        dispatcher.dispatch_next(
            POST, selection.model_copy(update={"stable_method_fingerprint": "f" * 64})
        )
    assert error.value.code == "CONFLICT" and dispatcher.status(progress.key) is None
    with pytest.raises(DomainError):
        dispatcher.dispatch_next(POST, selection.model_copy(update={"include_closed_runs": True}))
    assert pipeline.plans.progress(progress.key) == progress


def test_automatic_batches_keep_qualified_prefixes_when_a_later_tail_needs_review(stable_dispatch):
    dispatcher, selection, pipeline, _, _ = stable_dispatch
    method = StableResidencyLayoutMethod(max_intervals=2)
    pipeline.plans.evidence.method = method
    pipeline.configuration = pipeline.configuration.model_copy(update={"method": method})
    closed = pipeline.consume(POST, incoming(pipeline)).batch
    key = closed.outcomes[0].layout_key
    selection = selection.model_copy(update={"stable_method_fingerprint": method.fingerprint})
    first = dispatcher.dispatch_next(POST, selection, limit=8)
    assert first.state == "enqueued" and len(first.dispatch.job_ids) == 3
    assert first.dispatch.plan.next_window == 3
    tail = dispatcher.dispatch_next(POST, selection, limit=8)
    assert tail.state == "blocked" and tail.attempt.last_error_code == "USAGE_NEEDS_REVIEW"
    assert pipeline.plans.progress(key).next_window == 3


def test_dispatch_attempt_migration_guards_and_atomic_marker_with_queue_progress(
    stable_dispatch, monkeypatch
):
    dispatcher, selection, pipeline, progress, _ = stable_dispatch
    with monkeypatch.context() as environment:
        environment.setenv(
            "ARIS_DATABASE_URL",
            pipeline.sessions.kw["bind"].url.render_as_string(hide_password=False),
        )
        command.downgrade(Config("alembic.ini"), "0037")
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
    original = dispatcher._persist

    def interrupted(*args):
        original(*args)
        raise RuntimeError("fixture failed after success marker")

    with monkeypatch.context() as patch:
        patch.setattr(dispatcher, "_persist", interrupted)
        with pytest.raises(RuntimeError, match="success marker"):
            dispatcher.dispatch_next(POST, selection)
    assert dispatcher.status(progress.key) is None
    assert pipeline.plans.progress(progress.key) == progress
    result = dispatcher.dispatch_next(POST, selection)
    assert (
        result.state == "enqueued"
        and result.attempt.observed_plan_version == result.dispatch.plan.version
    )
    for statement in (
        update(StableDispatchStateRow).values(version=1),
        delete(StableDispatchStateRow),
    ):
        with pytest.raises(DBAPIError), pipeline.sessions.begin() as db:
            db.execute(statement)
    if pipeline.sessions.kw["bind"].dialect.name == "postgresql":
        with pytest.raises(DBAPIError), pipeline.sessions.begin() as db:
            db.execute(text("TRUNCATE usage_stable_dispatch_states"))
    with monkeypatch.context() as environment:
        environment.setenv(
            "ARIS_DATABASE_URL",
            pipeline.sessions.kw["bind"].url.render_as_string(hide_password=False),
        )
        with pytest.raises(RuntimeError, match="stable dispatch history prevents downgrade"):
            command.downgrade(Config("alembic.ini"), "0037")


def test_postgresql_busy_advisory_plan_is_skipped_without_reverse_row_lock_wait(
    stable_dispatch, database_url
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real advisory locks and separate connections")
    dispatcher, selection, pipeline, first, _ = stable_dispatch
    second = prepare(second_source(pipeline))
    blocked_key, ready_key = min(first.key, second.key), max(first.key, second.key)
    with pipeline.sessions() as owner:
        StableWindowPlanRepository(owner).lock(blocked_key)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(dispatcher.dispatch_next, POST, selection).result(timeout=5)
        assert result.state == "enqueued" and result.dispatch.plan.key == ready_key
        assert pipeline.plans.progress(blocked_key).next_window == 0
        owner.rollback()
    result = dispatcher.dispatch_next(POST, selection)
    assert result.state == "enqueued" and result.dispatch.plan.key == blocked_key
