from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.lyra.models import UsageRecordingJobRow
from aris.modules.lyra.recording_job_repo import RecordingJobRepository
from aris.modules.lyra.recording_job_schemas import (
    RecordingJobClaim,
    RecordingJobLimits,
    StoredRecordingJob,
)
from aris.modules.lyra.recording_job_service import RecordingJobService
from tests.test_closed_run_recording import (
    admin_state,
    closed,
    counts,
    derivations,
    history_state,
    ledger,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "history_state", "derivations", "ledger", "closed"]
LIMITS = RecordingJobLimits(
    transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
)


@pytest.fixture
def jobs(closed):
    writer, request, _, _ = closed
    clock = closed[2][2][2][5]
    queue = RecordingJobService(writer.sessions, writer, limits=LIMITS, clock=lambda: clock["now"])
    return queue, request, closed, clock


def postgres(jobs):
    if jobs[0].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL recording job concurrency")


def size(queue):
    with queue.sessions() as session:
        return session.scalar(select(func.count()).select_from(UsageRecordingJobRow))


def test_fixed_job_queues_then_atomically_completes_one_real_measured_record(jobs):
    queue, request, state, _ = jobs
    before = counts(state)
    first = queue.enqueue(POST, request)
    assert not first.duplicate and first.job.state == "queued" and first.job.attempts == 0
    assert counts(state)[:6] == before[:6] and not first.job.quota_usage_available
    claim = queue.claim(POST)
    assert isinstance(claim, RecordingJobClaim)
    result = queue.execute(POST, claim)
    job = queue.exact(request.id)
    assert result.state == job.state == "completed" and result.recording_id == request.id
    assert job.request == request and job.version == 3 and job.attempts == 1
    assert counts(state)[:6] == tuple(
        a + b for a, b in zip(before[:6], (1, 1, 1, 1, 1, 1), strict=True)
    )
    committed = counts(state)
    assert queue.execute(POST, claim) == result and queue.claim(POST) is None
    assert queue.enqueue(POST, request).duplicate and counts(state) == committed
    with queue.sessions() as session:
        events = list(
            session.scalars(
                select(DomainEvent)
                .where(DomainEvent.aggregate_type == "usage_recording_job")
                .order_by(DomainEvent.aggregate_version)
            )
        )
    assert [row.aggregate_version for row in events] == [1, 2, 3]
    assert "linux-user" not in str([row.payload for row in events])


def test_paused_queue_keeps_history_and_refuses_new_work_without_rewriting_requests(jobs):
    queue, request, _, _ = jobs
    queue.enqueue(POST, request)
    queue.limits = None
    assert queue.exact(request.id).state == "queued" and queue.enqueue(POST, request).duplicate
    for operation in (
        lambda: queue.claim(POST),
        lambda: queue.enqueue(POST, request.model_copy(update={"id": uuid4()})),
    ):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "USAGE_NOT_CONFIGURED"
    queue.limits = LIMITS
    assert queue.execute(POST, queue.claim(POST)).state == "completed"
    queue.limits = None
    assert queue.exact(request.id).state == "completed" and queue.enqueue(POST, request).duplicate


def test_same_id_different_frozen_input_is_a_conflict_not_a_new_job(jobs):
    queue, request, _, _ = jobs
    queue.enqueue(POST, request)
    with pytest.raises(DomainError) as error:
        queue.enqueue(
            POST, request.model_copy(update={"identity_revision": request.identity_revision + 1})
        )
    assert error.value.code == "CONFLICT" and size(queue) == 1
    assert queue.exact(request.id).request == request


def test_expired_lease_is_reclaimed_and_old_owner_cannot_complete_or_register_failure(jobs):
    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    old = queue.claim(POST)
    assert queue.claim(POST) is None
    clock["now"] += timedelta(seconds=61)
    new = queue.claim(POST)
    assert new.attempt == 2 and new.lease_token != old.lease_token
    before = counts(state)
    version = queue.exact(request.id).version
    assert queue.execute(POST, old).state == "lost_claim"
    assert queue._register_failure(POST, old, "CONFLICT", LIMITS).state == "lost_claim"
    assert counts(state) == before and queue.exact(request.id).version == version
    assert queue.execute(POST, new).state == "completed"


def test_retry_keeps_the_exact_cutoff_identity_and_operation_and_uses_bounded_backoff(
    jobs, monkeypatch
):
    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    claim = queue.claim(POST)
    before = counts(state)[:6]
    seen = []

    def unavailable(session, context, value):
        seen.append(value)
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture original archive unavailable", 503)

    with monkeypatch.context() as patch:
        patch.setattr(queue.recorder, "record_in_transaction", unavailable)
        assert queue.execute(POST, claim).state == "retry"
    assert counts(state)[:6] == before and queue.claim(POST) is None
    assert queue.exact(request.id).available_at == clock["now"] + timedelta(seconds=1)
    clock["now"] += timedelta(seconds=1)
    second = queue.claim(POST)
    assert second.attempt == 2 and queue.execute(POST, second).state == "completed"
    assert seen == [request] and queue.exact(request.id).request == request


def test_retry_exhaustion_is_a_visible_failure_and_never_zero_or_completed_usage(jobs, monkeypatch):
    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    before = counts(state)[:6]

    def unavailable(*args):
        raise DomainError("RETRYABLE_DATABASE_ERROR", "fixture temporary failure", 503)

    monkeypatch.setattr(queue.recorder, "record_in_transaction", unavailable)
    for attempt in range(1, 4):
        claim = queue.claim(POST)
        result = queue.execute(POST, claim)
        assert claim.attempt == attempt and result.state == ("failed" if attempt == 3 else "retry")
        clock["now"] += timedelta(seconds=2 ** (attempt - 1))
    job = queue.exact(request.id)
    assert job.state == "failed" and job.recording_id is None and not job.quota_usage_available
    assert queue.claim(POST) is None and counts(state)[:6] == before


def test_known_invalid_run_is_blocked_and_retains_its_original_request(jobs):
    queue, request, state, _ = jobs
    bad = request.model_copy(
        update={"reference": request.reference.model_copy(update={"run_id": "f" * 64})}
    )
    queue.enqueue(POST, bad)
    before = counts(state)[:6]
    result = queue.execute(POST, queue.claim(POST))
    assert result.state == "blocked" and result.code == "NOT_FOUND"
    assert queue.exact(request.id).request == bad and counts(state)[:6] == before
    assert queue.claim(POST) is None


def test_repeated_worker_deaths_exhaust_leases_without_replacing_the_operation(jobs):
    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    before = counts(state)[:6]
    for attempt in range(1, 4):
        claim = queue.claim(POST)
        assert claim.attempt == attempt
        clock["now"] += timedelta(seconds=61)
    result = queue.claim(POST)
    assert result.state == "failed" and result.code == "ATTEMPTS_EXHAUSTED"
    assert queue.exact(request.id).request == request
    assert counts(state)[:6] == before and queue.claim(POST) is None


@pytest.mark.parametrize("change", ["pause", "expire"])
def test_pause_or_expiry_after_measured_writes_rolls_back_all_business_effects(
    jobs, monkeypatch, change
):
    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    claim = queue.claim(POST)
    before = counts(state)[:6]
    original = queue.recorder.record_in_transaction

    def changed(*args):
        result = original(*args)
        if change == "pause":
            queue.limits = None
        else:
            clock["now"] += timedelta(seconds=61)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(queue.recorder, "record_in_transaction", changed)
        if change == "pause":
            with pytest.raises(DomainError) as error:
                queue.execute(POST, claim)
            assert error.value.code == "USAGE_NOT_CONFIGURED"
        else:
            assert queue.execute(POST, claim).state == "lost_claim"
    assert counts(state)[:6] == before and queue.exact(request.id).state == "running"
    queue.limits = LIMITS
    next_claim = claim if change == "pause" else queue.claim(POST)
    assert queue.execute(POST, next_claim).state == "completed"


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_execute_commit_recovers_one_job_and_one_ledger_measurement(
    jobs, monkeypatch, when
):
    import aris.modules.lyra.recording_job_service as module

    queue, request, state, clock = jobs
    queue.enqueue(POST, request)
    claim = queue.claim(POST)
    before = counts(state)[:6]
    original = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            original(session)
        raise RuntimeError("fixture unknown job commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError):
            queue.execute(POST, claim)
    assert queue.exact(request.id).state == ("completed" if when == "after" else "running")
    if when == "before":
        clock["now"] += timedelta(seconds=61)
        claim = queue.claim(POST)
    assert queue.execute(POST, claim).state == "completed"
    assert counts(state)[:6] == tuple(
        a + b for a, b in zip(before, (1, 1, 1, 1, 1, 1), strict=True)
    )


def test_job_completion_update_failure_cannot_leave_only_its_ledger_committed(jobs):
    queue, request, state, _ = jobs
    queue.enqueue(POST, request)
    claim = queue.claim(POST)
    before = counts(state)
    engine = queue.sessions.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE usage_recording_jobs"):
            raise RuntimeError("fixture interrupted job completion")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            queue.execute(POST, claim)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(state) == before and queue.exact(request.id).state == "running"
    assert queue.execute(POST, claim).state == "completed"


def test_enqueue_and_outer_transaction_rollback_keep_no_partial_job(jobs):
    queue, request, state, _ = jobs
    before = counts(state)
    with queue.sessions() as session:
        assert queue.enqueue_in_transaction(session, POST, request).job.state == "queued"
        session.rollback()
    assert size(queue) == 0 and counts(state) == before
    assert not queue.enqueue(POST, request).duplicate


def test_empty_migration_roundtrip_and_nonempty_input_history_guards(jobs):
    queue, request, _, _ = jobs
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0020")
    command.upgrade(cfg, "head")
    command.check(cfg)
    queue.enqueue(POST, request)
    for sql in (
        "DELETE FROM usage_recording_jobs",
        "UPDATE usage_recording_jobs SET request_fingerprint='changed'",
        "UPDATE usage_recording_jobs SET state='completed',finished_at=updated_at",
    ):
        with pytest.raises(DBAPIError), queue.sessions.begin() as session:
            session.execute(text(sql))
    with pytest.raises(RuntimeError, match="recording job history prevents downgrade"):
        command.downgrade(cfg, "0020")
    assert queue.exact(request.id).state == "queued"


def test_limits_and_job_contract_cannot_forge_a_finished_result_or_unsafe_lease(jobs):
    queue, request, _, _ = jobs
    original = queue.enqueue(POST, request).job
    for changes in (
        {"transaction_timeout_ms": 60000},
        {"lease_seconds": 30},
        {"max_attempts": True},
    ):
        with pytest.raises(ValidationError):
            RecordingJobLimits.model_validate({**LIMITS.model_dump(), **changes})
    for changes in (
        {"state": "completed"},
        {"quota_usage_available": True},
        {"attempts": -1},
        {"request_fingerprint": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            StoredRecordingJob.model_validate({**original.model_dump(), **changes})


def test_postgresql_same_key_enqueue_and_execute_append_one_result(jobs):
    queue, request, state, _ = jobs
    postgres(jobs)
    gate = Barrier(2)

    def enqueue(_):
        gate.wait(timeout=5)
        return queue.enqueue(POST, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, range(2)))
    assert sorted(result.duplicate for result in results) == [False, True] and size(queue) == 1
    claim = queue.claim(POST)
    before = counts(state)[:6]
    gate = Barrier(2)

    def execute(_):
        gate.wait(timeout=5)
        return queue.execute(POST, claim)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert all(result.state == "completed" for result in results)
    assert counts(state)[:6] == tuple(
        a + b for a, b in zip(before, (1, 1, 1, 1, 1, 1), strict=True)
    )


def test_postgresql_skip_locked_claims_other_job_and_distinct_jobs_share_one_measurement(jobs):
    queue, request, state, _ = jobs
    postgres(jobs)
    other = request.model_copy(update={"id": uuid4()})
    for value in (request, other):
        queue.enqueue(POST, value)
    first_id = min((request.id, other.id), key=str)
    with queue.sessions() as held:
        assert RecordingJobRepository(held).locked(first_id)
        claim = queue.claim(POST)
        assert claim.id != first_id
    second = queue.claim(POST)
    assert second.id == first_id
    assert queue.execute(POST, claim).state == queue.execute(POST, second).state == "completed"
    assert counts(state)[2] == 1


def test_postgresql_uncommitted_job_is_invisible_and_read_only_query_preserves_owner_budget(jobs):
    queue, request, _, _ = jobs
    postgres(jobs)
    with queue.sessions() as writer:
        queue.enqueue_in_transaction(writer, POST, request)
        with pytest.raises(DomainError) as error:
            queue.exact(request.id)
        assert error.value.code == "NOT_FOUND"
        writer.commit()
    with queue.sessions() as reader:
        reader.execute(text("SET TRANSACTION READ ONLY"))
        reader.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        reader.execute(text("SET LOCAL statement_timeout='750ms'"))
        row = reader.get(UsageRecordingJobRow, str(request.id))
        row.canonical_request = "pending invalid content"

        def reject(*args):
            raise AssertionError("job query must not flush owner work")

        event.listen(reader, "before_flush", reject)
        try:
            assert queue.exact_in_transaction(reader, request.id).request == request
            assert reader.scalar(text("SHOW transaction_timeout")) == "2s"
            assert reader.scalar(text("SHOW statement_timeout")) == "750ms"
        finally:
            event.remove(reader, "before_flush", reject)
    with pytest.raises(DBAPIError), queue.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_recording_jobs CASCADE"))


def test_postgresql_original_archive_timeout_retries_without_partial_measured_rows(jobs):
    queue, request, state, clock = jobs
    postgres(jobs)
    queue.enqueue(POST, request)
    claim = queue.claim(POST)
    before = counts(state)[:6]
    with queue.sessions.begin() as held:
        held.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        result = queue.execute(POST, claim)
        assert result.state == "retry" and result.code == "RETRYABLE_DATABASE_ERROR"
    assert counts(state)[:6] == before
    clock["now"] += timedelta(seconds=1)
    assert queue.execute(POST, queue.claim(POST)).state == "completed"
