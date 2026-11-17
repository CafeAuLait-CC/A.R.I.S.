import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from aris.core.access.models import User
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandIntakeReceipt, CommandJob, CommandRecord
from aris.core.commands.queue_schemas import CommandClaim, WithdrawCommand
from aris.core.commands.worker import CommandWorker
from aris.core.contracts import DomainError
from aris.core.time import utc
from tests.test_command_intake import (
    KEY,
    LIMITS,
    WORKER_LIMITS,
    actor,
    context,
    queued,
    stored_counts,
)
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state
from tests.test_commands import request


def test_transient_database_failure_rolls_back_before_bounded_same_key_retry(durable_state):
    intake, sessions, ids, action, clock = durable_state
    _, accepted = queued(durable_state)
    original = action.execute
    attempts = []

    def contended(session, execution, principal, parameters):
        result = original(session, execution, principal, parameters)
        session.flush()
        attempts.append(execution.command_id)
        if len(attempts) < 2:
            raise OperationalError("fixture", None, LockNotAvailable("private-database-detail"))
        return result

    action.execute = contended
    worker = CommandWorker(intake, WORKER_LIMITS)
    first = worker.run_once()
    assert first.state == "retry" and first.code == "RETRYABLE_DATABASE_ERROR"
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        job = session.scalar(select(CommandJob))
        assert job.state == "queued" and job.attempts == 1
        assert utc(job.available_at) == clock["now"] + timedelta(seconds=1)
        assert "command.succeeded" not in session.scalars(select(AuditEntry.action)).all()
    assert worker.run_once() is None
    clock["now"] += timedelta(seconds=1)
    assert worker.run_once().state == "succeeded"
    assert attempts == [accepted.outcome.command_id] * 2
    assert stored_counts(sessions) == (1, 1, 1, 2)
    assert "private-database-detail" not in repr(intake.inspect(actor(ids), attempts[0]))


def test_retry_attempts_and_original_deadline_bound_failure_recovery(durable_state):
    intake, sessions, ids, action, clock = durable_state
    queued(durable_state)

    def always_busy(*args):
        raise OperationalError("fixture", None, LockNotAvailable("private-detail"))

    action.execute = always_busy
    worker = CommandWorker(intake, WORKER_LIMITS)
    for attempt in range(1, 4):
        result = worker.run_once()
        assert result.state == ("failed" if attempt == 3 else "retry")
        with sessions() as session:
            job = session.scalar(select(CommandJob))
            assert job.attempts == attempt
            clock["now"] = utc(job.available_at)
    assert worker.run_once() is None
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"


def test_abandoned_claims_exhaust_attempts_without_business_execution(durable_state):
    intake, _, _, action, clock = durable_state
    queued(durable_state)
    worker = CommandWorker(intake, WORKER_LIMITS)
    for attempt in range(1, 4):
        claim = worker.claim()
        assert isinstance(claim, CommandClaim) and claim.attempt == attempt
        clock["now"] = claim.lease_expires_at
    result = worker.claim()
    assert result.state == "failed" and result.code == "ATTEMPTS_EXHAUSTED"
    assert worker.run_once() is None and action.calls == 0


def test_withdraw_after_claim_fences_worker_before_any_business_work(durable_state):
    intake, _, ids, action, _ = durable_state
    _, accepted = queued(durable_state)
    worker = CommandWorker(intake, WORKER_LIMITS)
    claim = worker.claim()
    result = intake.withdraw(
        actor(ids, receipt="withdraw"), WithdrawCommand(command_id=accepted.outcome.command_id)
    )
    assert result.outcome.state == "cancelled"
    assert worker.execute(claim).state == "cancelled" and action.calls == 0


def test_deadline_is_checked_at_connection_commit_after_flush(durable_state):
    intake, sessions, ids, action, clock = durable_state
    _, accepted = queued(durable_state)
    clock["now"] = accepted.outcome.execute_by - timedelta(seconds=1)
    worker = CommandWorker(intake, WORKER_LIMITS)
    claim = worker.claim()
    original = action.execute

    def delayed_commit(session, execution, principal, parameters):
        result = original(session, execution, principal, parameters)

        def expire(active):
            clock["now"] = accepted.outcome.execute_by

        event.listen(session, "before_commit", expire, once=True)
        return result

    action.execute = delayed_commit
    assert worker.execute(claim).state == "expired"
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert "command.succeeded" not in session.scalars(select(AuditEntry.action)).all()


def test_uncertain_admission_commit_does_not_return_an_accepted_receipt(durable_state):
    intake, sessions, ids, action, _ = durable_state
    engine = sessions.kw["bind"]

    def interrupted(connection):
        raise OperationalError("fixture", None, LockNotAvailable("private-commit-detail"))

    event.listen(engine, "commit", interrupted, once=True)
    try:
        with pytest.raises(DomainError) as failed:
            intake.submit(context(ids), request())
        assert failed.value.code == "INTAKE_UNAVAILABLE"
    finally:
        event.remove(engine, "commit", interrupted)
    assert stored_counts(sessions) == (0, 0, 0, 0) and action.calls == 0
    assert intake.submit(context(ids), request()).disposition == "accepted"


@pytest.mark.parametrize("phase", ["claimed", "business_flushed", "committed"])
def test_killed_fixture_worker_recovers_without_partial_or_duplicate_business(
    durable_state, settings, phase
):
    intake, sessions, ids, action, clock = durable_state
    _, accepted = queued(durable_state)
    program = textwrap.dedent("""
        import json, os, sys
        from datetime import datetime
        from pydantic import SecretStr
        from aris.core.db import build_engine, session_factory
        from aris.core.commands.service import CommandService
        from aris.core.commands.intake import CommandIntakeService
        from aris.core.commands.worker import CommandWorker
        from aris.core.commands.schemas import CommandDefinition
        from tests.test_commands import RenameFixture
        from tests.test_command_intake import NAMESPACE, LIMITS, WORKER_LIMITS

        data = json.loads(os.environ['ARIS_FIXTURE_INPUT'])
        class Fixture(RenameFixture):
            definition = CommandDefinition(name='profile.rename',
                required_permission='profile.write')
            def execute(self, session, execution, principal, parameters):
                result = super().execute(session, execution, principal, parameters)
                if data['phase'] == 'business_flushed':
                    session.flush()
                    print(json.dumps({'phase':'business_flushed'}), flush=True)
                    sys.stdin.read()
                return result

        engine = build_engine(os.environ['ARIS_FIXTURE_DATABASE_URL'])
        commands = CommandService(session_factory(engine), [Fixture()],
            confirmation_key=SecretStr(data['key']),
            clock=lambda: datetime.fromisoformat(data['now']))
        intake = CommandIntakeService(commands, NAMESPACE, LIMITS,
            confirmation_key_id='fixture-key-1')
        worker = CommandWorker(intake, WORKER_LIMITS)
        claim = worker.claim()
        if data['phase'] == 'claimed':
            print(json.dumps({'phase':'claimed'}), flush=True)
            sys.stdin.read()
        result = worker.execute(claim)
        if data['phase'] == 'committed':
            assert result.state == 'succeeded'
            print(json.dumps({'phase':'committed'}), flush=True)
            sys.stdin.read()
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "ARIS_FIXTURE_DATABASE_URL": settings.database_url,
            "ARIS_FIXTURE_INPUT": json.dumps(
                {"phase": phase, "key": KEY.get_secret_value(), "now": clock["now"].isoformat()}
            ),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "fixture did not reach its crash point"
        assert json.loads(child.stdout.readline()) == {"phase": phase}
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    with sessions() as session:
        record = session.get(CommandRecord, str(accepted.outcome.command_id))
        assert record.state == ("succeeded" if phase == "committed" else "queued")
        assert session.get(User, ids["alice"]).display_name == (
            "Renamed" if phase == "committed" else "Original A"
        )
        job = session.scalar(select(CommandJob))
        if phase != "committed":
            clock["now"] = utc(job.lease_expires_at)
    worker = CommandWorker(intake, WORKER_LIMITS)
    if phase == "committed":
        assert worker.run_once() is None and action.calls == 0
    else:
        assert worker.run_once().state == "succeeded" and action.calls == 1
    assert intake.inspect(actor(ids), accepted.outcome.command_id).state == "succeeded"
    with sessions() as session:
        assert list(session.scalars(select(AuditEntry.action))).count("command.succeeded") == 1
    assert stored_counts(sessions) == (1, 1, 1, 2)


def require_postgres(sessions):
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL lock waiting, SKIP LOCKED and transaction budgets")


@pytest.mark.parametrize("same_receipt", [False, True])
def test_postgres_duplicate_concurrent_admission_creates_one_intent(durable_state, same_receipt):
    intake, sessions, ids, action, _ = durable_state
    require_postgres(sessions)
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        return intake.submit(
            context(ids, receipt="submit" if same_receipt else "submit-" + str(index)), request()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    for index, item in enumerate(results):
        if item.error_code == "QUEUE_BUSY":
            results[index] = intake.submit(
                context(ids, receipt="submit" if same_receipt else "submit-" + str(index)),
                request(),
            )
    assert all(item.disposition == "accepted" for item in results)
    assert len({item.outcome.command_id for item in results}) == 1 and action.calls == 0
    assert stored_counts(sessions) == (1, 1, 0, 1 if same_receipt else 2)


def test_postgres_capacity_guard_never_exceeds_bound_during_new_admission_race(durable_state):
    intake, sessions, ids, _, _ = durable_state
    require_postgres(sessions)
    intake.limits = LIMITS.model_copy(update={"max_pending_commands": 1})
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        return intake.submit(
            context(ids, receipt=f"submit-{index}", key=f"intent-{index}"), request()
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(item.disposition == "accepted" for item in results) == 1
    assert next(item.error_code for item in results if item.error_code) in {
        "QUEUE_BUSY",
        "QUEUE_FULL",
    }
    assert stored_counts(sessions) == (1, 1, 0, 2)


def test_postgres_two_workers_execute_a_queued_command_only_once(durable_state):
    intake, sessions, _, action, _ = durable_state
    require_postgres(sessions)
    queued(durable_state)
    barrier = Barrier(2)

    def work(index):
        barrier.wait(timeout=5)
        return CommandWorker(intake, WORKER_LIMITS).run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, range(2)))
    assert len([item for item in results if item is not None]) == 1
    assert next(item for item in results if item is not None).state == "succeeded"
    assert action.calls == 1


def test_postgres_short_admission_timeout_keeps_original_receipt_and_allows_retry(durable_state):
    intake, sessions, ids, action, _ = durable_state
    require_postgres(sessions)
    pending = intake.submit(context(ids), request())
    with sessions.begin() as lock:
        lock.execute(
            text("SELECT id FROM command_intake_receipts WHERE id=:id FOR UPDATE"),
            {"id": str(pending.receipt_id)},
        ).one()
        started = time.monotonic()
        with pytest.raises(DomainError) as busy:
            intake.submit(context(ids), request())
        assert busy.value.code == "INTAKE_UNAVAILABLE"
        assert time.monotonic() - started < 2
    replay = intake.submit(context(ids), request())
    assert replay.disposition == "accepted" and replay.duplicate
    assert replay.outcome == pending.outcome and action.calls == 0
    with sessions() as session:
        assert session.scalar(select(CommandIntakeReceipt)).state == "accepted"


def test_postgres_skip_locked_workers_can_claim_another_job(durable_state):
    intake, sessions, _, action, _ = durable_state
    require_postgres(sessions)
    queued(durable_state, key="one")
    queued(durable_state, key="two")
    with sessions.begin() as lock:
        held = lock.scalar(select(CommandJob).order_by(CommandJob.id).limit(1).with_for_update())
        result = CommandWorker(intake, WORKER_LIMITS).run_once()
        assert result.state == "succeeded" and str(result.job_id) != held.id
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    assert action.calls == 2


def test_postgres_scope_statement_timeout_is_short_audited_and_retryable(durable_state):
    intake, sessions, ids, action, _ = durable_state
    require_postgres(sessions)
    original = action.authorization_scope

    def slow_scope(session, parameters):
        session.execute(text("SELECT pg_sleep(2)"))
        return original(session, parameters)

    action.authorization_scope = slow_scope
    started = time.monotonic()
    result = intake.submit(context(ids), request())
    assert result.disposition == "rejected" and result.error_code == "RETRYABLE_DATABASE_ERROR"
    assert result.retryable and time.monotonic() - started < 2
    assert stored_counts(sessions) == (0, 0, 0, 1)
    action.authorization_scope = original
    retried = intake.submit(context(ids), request())
    assert retried.disposition == "accepted" and retried.duplicate
    assert action.calls == 0


def test_postgres_worker_lock_timeout_rolls_back_and_retries_original_job(durable_state):
    intake, sessions, ids, action, clock = durable_state
    require_postgres(sessions)
    queued(durable_state)
    with sessions.begin() as lock:
        lock.execute(
            text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": ids["alice"]}
        ).one()
        started = time.monotonic()
        result = CommandWorker(intake, WORKER_LIMITS).run_once()
        assert result.state == "retry" and result.code == "RETRYABLE_DATABASE_ERROR"
        assert time.monotonic() - started < 2
        assert lock.get(User, ids["alice"]).display_name == "Original A"
    clock["now"] += timedelta(seconds=1)
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Renamed"
        assert session.scalar(select(CommandJob)).attempts == 2
