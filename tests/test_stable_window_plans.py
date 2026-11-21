import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.modules.lyra.models import StableWindowPlanRow, UsageRecordingJobRow
from aris.modules.lyra.recording_job_schemas import RecordingJobClaim
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.stable_window_plan_schemas import (
    StableWindowDispatchRequest,
    StableWindowPlanObservation,
)
from aris.modules.lyra.stable_window_plan_service import StableWindowPlanService
from tests.test_recording_jobs import LIMITS
from tests.test_stable_window_acceptance import counts, open_reference
from tests.test_stable_window_recording import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
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


@pytest.fixture
def stable_plans(stable_recording):
    writer, closed, (app, _, fixture) = stable_recording
    jobs = RecordingJobService(
        writer.sessions, fixture[3], stable_recorder=writer, limits=LIMITS, clock=app.clock
    )
    plans = StableWindowPlanService(
        writer.sessions,
        app.evidence,
        app,
        jobs,
        configuration=app.configuration,
        clock=app.clock,
    )
    request = StableWindowPlanObservation.model_validate(closed.model_dump())
    return plans, request, writer, app, fixture


def dispatch(plans, progress, limit=8):
    return plans.dispatch(
        POST,
        StableWindowDispatchRequest(
            key=progress.key,
            expected_version=progress.version,
            limit=limit,
        ),
    )


def run_jobs(plans):
    outputs = []
    while (claim := plans.jobs.claim(POST)) is not None:
        assert isinstance(claim, RecordingJobClaim)
        result = plans.jobs.execute(POST, claim)
        assert result.state == "completed"
        outputs.append(result)
    return outputs


@pytest.mark.parametrize("capacity", [3, 8])
def test_growing_open_layout_then_real_closure_retains_progress_and_first_window(
    stable_plans, capacity
):
    plans, closed, writer, app, _ = stable_plans
    method = StableResidencyLayoutMethod(max_intervals=capacity)
    app.evidence.method = method
    closed = closed.model_copy(
        update={
            "window": closed.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    opened = StableWindowPlanObservation.model_validate(open_reference(app, closed).model_dump())
    first = plans.observe(POST, opened).plan
    before = counts(app)
    queued = dispatch(plans, first)
    assert len(queued.job_ids) == (1 if capacity == 3 else 0)
    assert counts(app)[-2:] == before[-2:]  # Dispatch is not measurement.
    first_acceptances = [app.exact(plans.jobs.exact(id).request.key) for id in queued.job_ids]
    run_jobs(plans)
    observed = plans.observe(POST, closed).plan
    assert observed.key == first.key and observed.origin == opened
    assert observed.next_window == queued.plan.next_window
    rebuilt = StableWindowPlanService(
        plans.sessions,
        plans.evidence,
        app,
        plans.jobs,
        configuration=plans.configuration,
        clock=plans.clock,
    )
    final = dispatch(rebuilt, observed)
    assert final.plan.next_window == final.plan.page.total_planned_windows
    assert len(final.job_ids) == 2
    assert all(app.exact(value.proof.window.key) == value for value in first_acceptances)
    assert plans.observe(POST, opened).duplicate and plans.progress(first.key) == final.plan
    completed = run_jobs(plans)
    with plans.sessions() as db:
        ids = list(db.scalars(select(UsageRecordingJobRow.id)))
    from aris.modules.lyra.ledger_repo import UsageLedgerRepository

    with plans.sessions() as db:
        operations = [
            UsageLedgerRepository(db).operation(id)
            for id in [plans.jobs.exact(id).request.id for id in ids]
        ]
    entries = [
        writer.ledger.exact(value.receipt.entries[0]).details.measurement for value in operations
    ]
    entries.sort(key=lambda value: value.start_at)
    assert sum(value.observed_microseconds for value in entries) == 135000000
    assert all(
        left.end_at == right.start_at for left, right in zip(entries, entries[1:], strict=False)
    )
    assert (
        not final.plan.quota_usage_available and final.plan.time_window_coverage == "not_assessed"
    )
    assert len(completed) == 2 and dispatch(plans, final.plan).job_ids == ()


def test_dispatch_failure_rolls_back_first_acceptances_jobs_and_progress(stable_plans, monkeypatch):
    plans, request, _, app, _ = stable_plans
    progress = plans.observe(POST, request).plan
    before = counts(app)
    original, called = plans.jobs.enqueue_in_transaction, []

    def fail(*args):
        result = original(*args)
        called.append(True)
        if len(called) == 2:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture second enqueue failed", 503)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(plans.jobs, "enqueue_in_transaction", fail)
        with pytest.raises(DomainError):
            dispatch(plans, progress)
    assert counts(app) == before and plans.progress(progress.key) == progress
    with plans.sessions() as db:
        assert db.scalar(select(func.count()).select_from(UsageRecordingJobRow)) == 0
    assert len(dispatch(plans, progress).job_ids) == 3


def test_short_final_tail_is_unresolved_and_never_advances_its_cursor(stable_plans):
    plans, request, _, app, _ = stable_plans
    method = StableResidencyLayoutMethod(max_intervals=2)
    app.evidence.method = method
    request = request.model_copy(
        update={
            "window": request.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    progress = plans.observe(POST, request).plan
    progress = dispatch(plans, progress, limit=3).plan
    assert progress.next_window == 3 and progress.page.total_planned_windows == 5
    before = counts(app)
    with pytest.raises(DomainError) as error:
        dispatch(plans, progress)
    assert error.value.code == "USAGE_NEEDS_REVIEW"
    assert plans.progress(progress.key) == progress and counts(app) == before


def test_migration_origin_and_progress_guards_and_final_observation_recheck(
    stable_plans, monkeypatch
):
    plans, request, _, _, _ = stable_plans
    with monkeypatch.context() as environment:
        environment.setenv(
            "ARIS_DATABASE_URL", plans.sessions.kw["bind"].url.render_as_string(hide_password=False)
        )
        command.downgrade(Config("alembic.ini"), "0036")
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
    original, calls = plans.evidence.layout_witness_in_transaction, []

    def changed(*args):
        calls.append(True)
        if len(calls) == 2:
            raise DomainError("CONFLICT", "fixture final source changed", 409)
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(plans.evidence, "layout_witness_in_transaction", changed)
        with pytest.raises(DomainError):
            plans.observe(POST, request)
    with plans.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StableWindowPlanRow)) == 0
    progress = plans.observe(POST, request).plan
    for statement in (
        update(StableWindowPlanRow).values(canonical_origin="{}", version=2),
        update(StableWindowPlanRow).values(version=1),
        update(StableWindowPlanRow).values(next_window=-1, version=2),
        delete(StableWindowPlanRow),
    ):
        with pytest.raises(DBAPIError), plans.sessions.begin() as db:
            db.execute(statement)
    if plans.sessions.kw["bind"].dialect.name == "postgresql":
        with pytest.raises(DBAPIError), plans.sessions.begin() as db:
            db.execute(text("TRUNCATE usage_stable_window_plans"))
    with monkeypatch.context() as environment:
        environment.setenv(
            "ARIS_DATABASE_URL", plans.sessions.kw["bind"].url.render_as_string(hide_password=False)
        )
        with pytest.raises(RuntimeError, match="stable plan history prevents downgrade"):
            command.downgrade(Config("alembic.ini"), "0036")
    assert plans.progress(progress.key) == progress


def test_postgresql_competing_versions_only_dispatch_once(stable_plans, database_url):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real advisory locking")
    plans, request, _, app, _ = stable_plans
    progress = plans.observe(POST, request).plan
    before = counts(app)

    def attempt(_):
        try:
            return dispatch(plans, progress, limit=1)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(value == "CONFLICT" for value in results) == 1
    assert plans.progress(progress.key).next_window == 1
    assert counts(app)[-2:] == before[-2:]


@pytest.mark.parametrize("partition_recording", [1030], indirect=True)
@pytest.mark.skipif(
    not os.environ.get("ARIS_TEST_DATABASE_URL"), reason="long integration runs on PostgreSQL"
)
def test_long_open_run_dispatches_in_eight_window_batches_and_records_the_exact_closed_tail(
    stable_plans,
):
    plans, request, writer, app, _ = stable_plans
    method = StableResidencyLayoutMethod(max_intervals=64)
    app.evidence.method = method
    request = request.model_copy(
        update={
            "window": request.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    opened = StableWindowPlanObservation.model_validate(
        open_reference(app, request, from_end=True).model_dump()
    )
    progress = plans.observe(POST, opened).plan
    assert progress.witness.parent_run.intervals > 1024
    first = dispatch(plans, progress)
    assert len(first.job_ids) == 8 and first.plan.next_window == 8
    second = dispatch(plans, first.plan)
    assert len(second.job_ids) == 7 and second.plan.next_window == 15
    before_close = run_jobs(plans)
    progress = plans.observe(POST, request).plan
    tail = dispatch(plans, progress)
    assert (
        len(tail.job_ids) == 2
        and tail.plan.next_window == tail.plan.page.total_planned_windows == 17
    )
    after_close = run_jobs(plans)
    assert len(before_close) == 15 and len(after_close) == 2
    from aris.modules.lyra.ledger_repo import UsageLedgerRepository

    with plans.sessions() as db:
        entries = [
            writer.ledger.exact(
                UsageLedgerRepository(db).operation(result.id).receipt.entries[0]
            ).details.measurement
            for result in before_close + after_close
        ]
    entries.sort(key=lambda item: item.start_at)
    assert sum(item.observed_microseconds for item in entries) == 1029 * 15000000
    assert all(
        left.end_at == right.start_at for left, right in zip(entries, entries[1:], strict=False)
    )
