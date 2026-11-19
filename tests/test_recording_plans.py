from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import UsageRecordingJobRow, UsageRecordingPlanRow
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.recording_plan_schemas import RecordingDispatchRequest, RecordingPlanRequest
from aris.modules.lyra.recording_plan_service import RecordingPlanService
from tests.test_partition_recording import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


@pytest.fixture
def plans(partition_recording):
    writer, original, _, whole, clock = partition_recording

    def now():
        return clock["now"]

    jobs = RecordingJobService(
        writer.sessions,
        whole,
        partition_recorder=writer,
        limits=RecordingJobLimits(
            transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
        ),
        clock=now,
    )
    service = RecordingPlanService(writer.sessions, writer.evidence, writer.ledger, jobs, clock=now)
    request = RecordingPlanRequest.model_validate(original.model_dump(mode="json"))
    return service, request, partition_recording


def counts(state):
    with state[0].sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                UsageRecordingPlanRow,
                UsageRecordingJobRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
            )
        )


def dispatch(service, plan, version, limit=1):
    return service.dispatch(
        POST,
        RecordingDispatchRequest(plan_id=plan.request.id, expected_version=version, limit=limit),
    )


def postgres(state):
    if state[0].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL recording plan locks and history protection")


def test_small_batches_preserve_stable_child_inputs_and_only_claim_enqueued_progress(plans):
    service, request, _ = plans
    plan = service.create(POST, request).plan
    assert plan.partition_count == 3 and not plan.quota_usage_available
    first = dispatch(service, plan, 1)
    assert first.job_ids == (plan.job(0).id,) and first.progress.next_partition == 1
    assert first.progress.version == 2 and not first.progress.all_enqueued
    end = dispatch(service, plan, 2, 8)
    assert end.job_ids == (plan.job(1).id, plan.job(2).id)
    assert end.progress.all_enqueued and end.progress.next_partition == 3
    assert end.progress.version == 3
    for index in range(3):
        job = service.jobs.exact(plan.job(index).id)
        assert job.request == plan.job(index) and job.state == "queued" and job.attempts == 0
    # Dispatch success does not write a measurement or pretend quota is ready.
    with service.sessions() as session:
        assert session.scalar(text("SELECT count(*) FROM usage_ledger_entries")) == 0
    for _ in range(3):
        claim = service.jobs.claim(POST)
        assert service.jobs.execute(POST, claim).state == "completed"
    with service.sessions() as session:
        assert (
            session.scalar(text("SELECT sum(observed_microseconds) FROM usage_ledger_entries"))
            == 135000000
        )
    assert not service.progress(plan.request.id).quota_usage_available
    assert dispatch(service, plan, 3).job_ids == ()


def test_paused_creation_replay_and_read_are_available_but_new_dispatch_requires_configuration(
    plans,
):
    service, request, _ = plans
    first = service.create(POST, request)
    before = counts(plans)
    service.ledger.configuration = None
    replay = service.create(POST, request)
    assert replay.duplicate and replay.plan == first.plan and counts(plans) == before
    assert service.progress(request.id).next_partition == 0
    with pytest.raises(DomainError) as error:
        dispatch(service, first.plan, 1)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(plans) == before
    with pytest.raises(DomainError) as error:
        service.create(POST, request.model_copy(update={"id": uuid4()}))
    assert error.value.code == "USAGE_NOT_CONFIGURED"
    with pytest.raises(DomainError) as error:
        service.create(
            POST, request.model_copy(update={"identity_revision": request.identity_revision + 1})
        )
    assert error.value.code == "CONFLICT"


def test_mid_batch_queue_failure_and_outer_rollback_never_skip_a_child(plans, monkeypatch):
    service, request, _ = plans
    plan = service.create(POST, request).plan
    before = counts(plans)
    original = service.jobs.enqueue_in_transaction
    calls = []

    def failed(*args):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("fixture second child interrupted")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(service.jobs, "enqueue_in_transaction", failed)
        with pytest.raises(RuntimeError):
            dispatch(service, plan, 1, 3)
    assert counts(plans) == before and service.progress(request.id).next_partition == 0
    with service.sessions() as session:
        service.dispatch_in_transaction(
            session, POST, RecordingDispatchRequest(plan_id=request.id, expected_version=1, limit=3)
        )
        session.rollback()
    assert counts(plans) == before
    assert dispatch(service, plan, 1, 3).job_ids == tuple(plan.job(i).id for i in range(3))


def test_conflicting_preexisting_child_cannot_advance_or_leave_an_earlier_child(plans):
    service, request, _ = plans
    plan = service.create(POST, request).plan
    service.jobs.enqueue(
        POST, plan.job(1).model_copy(update={"identity_revision": request.identity_revision + 1})
    )
    before = counts(plans)
    with pytest.raises(DomainError) as error:
        dispatch(service, plan, 1, 3)
    assert error.value.code == "CONFLICT" and counts(plans) == before
    assert service.progress(request.id).next_partition == 0
    with pytest.raises(DomainError) as error:
        service.jobs.exact(plan.job(0).id)
    assert error.value.code == "NOT_FOUND"


def test_plan_creation_participates_in_the_callers_transaction(plans):
    service, request, _ = plans
    before = counts(plans)
    with service.sessions() as session:
        assert not service.create_in_transaction(session, POST, request).duplicate
        session.rollback()
    assert counts(plans) == before
    assert not service.create(POST, request).duplicate


@pytest.mark.parametrize("operation", ["create", "dispatch"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_outcome_is_recovered_from_the_original_plan_progress(
    plans, monkeypatch, operation, when
):
    import aris.modules.lyra.recording_plan_service as module

    service, request, _ = plans
    plan = service.create(POST, request).plan if operation == "dispatch" else None
    commit = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            commit(session)
        raise RuntimeError("fixture plan commit outcome unknown")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError):
            if operation == "create":
                service.create(POST, request)
            else:
                dispatch(service, plan, 1, 2)
    if operation == "create":
        receipt = service.create(POST, request)
        assert receipt.duplicate == (when == "after")
        assert service.progress(request.id).version == 1 and counts(plans)[:2] == (1, 0)
    else:
        current = service.progress(request.id)
        assert current.next_partition == (2 if when == "after" else 0)
        if when == "before":
            assert dispatch(service, plan, current.version, 2).job_ids == (
                plan.job(0).id,
                plan.job(1).id,
            )
        else:
            with pytest.raises(DomainError) as error:
                dispatch(service, plan, 1, 2)
            assert error.value.code == "CONFLICT"
        assert counts(plans)[:2] == (1, 2)


@pytest.mark.parametrize("changed", ["clock", "ledger", "jobs"])
def test_late_configuration_or_clock_changes_roll_back_jobs_and_progress(
    plans, monkeypatch, changed
):
    service, request, state = plans
    plan = service.create(POST, request).plan
    before = counts(plans)
    emit = service._emit

    def changing(*args):
        emit(*args)
        if changed == "clock":
            state[4]["now"] -= timedelta(seconds=1)
        elif changed == "ledger":
            service.ledger.configuration = None
        else:
            service.jobs.limits = None

    monkeypatch.setattr(service, "_emit", changing)
    with pytest.raises(DomainError):
        dispatch(service, plan, 1, 2)
    assert counts(plans) == before and service.progress(request.id).next_partition == 0


def test_plan_cannot_choose_count_owner_quantity_or_a_later_initial_partition(plans):
    service, request, _ = plans
    for name in ("partition_count", "owner_id", "observed_microseconds", "parent_run"):
        with pytest.raises(ValidationError):
            RecordingPlanRequest.model_validate({**request.model_dump(mode="json"), name: "forged"})
    with pytest.raises(ValidationError):
        RecordingPlanRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "partition": request.partition.model_copy(update={"partition_index": 1}).model_dump(
                    mode="json"
                ),
            }
        )
    for limit in (0, 9, True):
        with pytest.raises(ValidationError):
            RecordingDispatchRequest(plan_id=request.id, expected_version=1, limit=limit)
    changed = service.ledger.configuration.model_copy(
        update={"recording_start_at": request.partition.reference.source_cutoff}
    )
    service.ledger.configuration = changed
    with pytest.raises(DomainError) as error:
        service.create(
            POST,
            request.model_copy(
                update={"expected_recording_configuration_fingerprint": changed.fingerprint}
            ),
        )
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(plans)[0] == 0


def test_plan_input_deletion_and_invalid_progress_updates_are_database_protected(plans):
    service, request, _ = plans
    service.create(POST, request)
    before = service.progress(request.id)
    for statement in (
        update(UsageRecordingPlanRow).values(canonical_plan="{}"),
        update(UsageRecordingPlanRow).values(version=2),
        delete(UsageRecordingPlanRow),
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as session:
            session.execute(statement)
    assert service.progress(request.id) == before


def test_postgresql_same_plan_and_version_arbitration_creates_one_batch(plans):
    postgres(plans)
    service, request, _ = plans
    barrier = Barrier(2)

    def create(_):
        barrier.wait(timeout=5)
        return service.create(POST, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(create, range(2)))
    assert sorted(value.duplicate for value in receipts) == [False, True]
    plan = receipts[0].plan
    barrier = Barrier(2)

    def advance(_):
        barrier.wait(timeout=5)
        try:
            return dispatch(service, plan, 1, 2).progress.next_partition
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, range(2)))
    assert set(results) == {2, "CONFLICT"} and counts(plans)[:2] == (1, 2)


def test_postgresql_plan_lock_timeout_preserves_pending_progress(plans):
    postgres(plans)
    service, request, _ = plans
    plan = service.create(POST, request).plan
    with service.sessions.begin() as holder:
        holder.execute(
            select(UsageRecordingPlanRow)
            .where(UsageRecordingPlanRow.id == str(request.id))
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(dispatch, service, plan, 1)
            with pytest.raises(DomainError) as error:
                future.result(timeout=5)
            assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert service.progress(request.id).next_partition == 0
    assert dispatch(service, plan, 1).progress.next_partition == 1


def test_postgresql_truncate_does_not_erase_plan_history(plans):
    postgres(plans)
    service, request, _ = plans
    service.create(POST, request)
    with pytest.raises(DBAPIError), service.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_recording_plans"))
    assert service.progress(request.id).version == 1
