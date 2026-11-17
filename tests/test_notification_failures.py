import json
import os
import selectors
import signal
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg.errors import LockNotAvailable
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from aris.core.contracts import DomainError
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.time import utc
from tests.test_event_relay import seed
from tests.test_notifications import command_state as command_state
from tests.test_notifications import counts, enqueue, intent
from tests.test_notifications import durable_state as durable_state
from tests.test_notifications import notification_state as notification_state
from tests.test_notifications import relay_state as relay_state


def test_receipt_commit_failure_preserves_sending_phase_without_resending_automatically(
    notification_state,
):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    engine = sessions.kw["bind"]
    original = transport.send

    def received_but_cannot_commit(prepared):
        receipt = original(prepared)

        def interrupted(connection):
            raise OperationalError("fixture", None, LockNotAvailable("private-receipt-error"))

        event.listen(engine, "commit", interrupted, once=True)
        return receipt

    transport.send = received_but_cannot_commit
    with pytest.raises(DomainError) as failed:
        dispatcher.run_once()
    assert failed.value.code == "NOTIFICATION_UNAVAILABLE" and len(transport.sent) == 1
    with sessions() as session:
        record = session.scalar(select(NotificationOutbox))
        assert record.state == "running"
        assert session.scalar(select(NotificationAttempt)).state == "sending"
        clock["now"] = utc(record.lease_expires_at)
    assert dispatcher.run_once().state == "uncertain"
    assert dispatcher.run_once() is None and len(transport.sent) == 1


@pytest.mark.parametrize("phase", ["claimed", "prepared", "external_returned", "committed"])
def test_killed_notification_fixture_distinguishes_safe_recovery_from_uncertainty(
    notification_state, settings, phase
):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    program = textwrap.dedent("""
        import json, os, sys
        from datetime import datetime
        from aris.core.db import build_engine, session_factory
        from aris.core.notifications.service import NotificationService
        from aris.core.notifications.dispatch import NotificationDispatcher
        from tests.test_notifications import FixtureTemplate, FixtureTransport, LIMITS, RECEIPT
        from tests.test_command_intake import NAMESPACE

        data = json.loads(os.environ['ARIS_FIXTURE_INPUT'])
        sessions = session_factory(build_engine(os.environ['ARIS_FIXTURE_DATABASE_URL']))
        service = NotificationService(sessions, NAMESPACE, [FixtureTemplate()],
            clock=lambda: datetime.fromisoformat(data['now']))
        transport = FixtureTransport(sessions)
        dispatcher = NotificationDispatcher(service, LIMITS, transport)
        claim = dispatcher.claim()
        if data['phase'] == 'claimed':
            print(json.dumps({'phase':'claimed'}), flush=True)
            sys.stdin.read()
        prepared = dispatcher.prepare(claim)
        if data['phase'] == 'prepared':
            print(json.dumps({'phase':'prepared'}), flush=True)
            sys.stdin.read()
        receipt = transport.send(prepared)
        if data['phase'] == 'external_returned':
            print(json.dumps({'phase':'external_returned'}), flush=True)
            sys.stdin.read()
        result = dispatcher.finish(claim, receipt=receipt)
        assert result.state == 'delivered'
        if data['phase'] == 'committed':
            print(json.dumps({'phase':'committed'}), flush=True)
            sys.stdin.read()
    """)
    child = subprocess.Popen(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "ARIS_FIXTURE_DATABASE_URL": settings.database_url,
            "ARIS_FIXTURE_INPUT": json.dumps({"phase": phase, "now": clock["now"].isoformat()}),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=10), "notification fixture did not reach crash point"
        assert json.loads(child.stdout.readline()) == {"phase": phase}
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    with sessions() as session:
        record = session.scalar(select(NotificationOutbox))
        if phase == "committed":
            assert record.state == "delivered"
        else:
            assert record.state == "running"
            clock["now"] = utc(record.lease_expires_at)
    if phase == "claimed":
        assert dispatcher.run_once().state == "delivered" and len(transport.sent) == 1
        assert counts(sessions) == (1, 2)
    elif phase == "committed":
        assert dispatcher.run_once() is None and not transport.sent
    else:
        assert dispatcher.run_once().state == "uncertain" and not transport.sent
        assert dispatcher.run_once() is None and counts(sessions) == (1, 1)


def require_postgres(sessions):
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL uniqueness and SKIP LOCKED")


@pytest.mark.parametrize("conflicting", [False, True])
def test_postgres_concurrent_enqueue_is_unique_and_conflicts_do_not_overwrite(
    notification_state, conflicting
):
    service, _, sessions, _, _, _, _, relay_state = notification_state
    require_postgres(sessions)
    event_id = seed(relay_state)
    barrier = Barrier(2)

    def submit(index):
        barrier.wait(timeout=5)
        try:
            with sessions.begin() as session:
                return service.enqueue(
                    session,
                    intent(
                        notification_state,
                        event_id,
                        f"Different {index}" if conflicting else "Same content",
                    ),
                )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    if conflicting:
        assert outcomes.count("CONFLICT") == 1
    else:
        assert sorted(item.duplicate for item in outcomes) == [False, True]
    assert counts(sessions) == (1, 0)


def test_postgres_two_notification_workers_only_start_one_send(notification_state):
    _, dispatcher, sessions, _, _, transport, _, _ = notification_state
    require_postgres(sessions)
    enqueue(notification_state)
    barrier = Barrier(2)

    def work(index):
        barrier.wait(timeout=5)
        return dispatcher.run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, range(2)))
    assert len([item for item in results if item is not None]) == 1
    assert next(item for item in results if item is not None).state == "delivered"
    assert len(transport.sent) == 1 and counts(sessions) == (1, 1)


def test_postgres_external_send_runs_after_preparation_releases_database_locks(notification_state):
    _, dispatcher, sessions, _, _, transport, _, _ = notification_state
    require_postgres(sessions)
    enqueue(notification_state)
    original = transport.send

    def no_transaction_held(prepared):
        with sessions.begin() as session:
            session.execute(
                text("SELECT id FROM notification_outbox WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": str(prepared.notification_id)},
            ).one()
        return original(prepared)

    transport.send = no_transaction_held
    assert dispatcher.run_once().state == "delivered" and len(transport.sent) == 1
