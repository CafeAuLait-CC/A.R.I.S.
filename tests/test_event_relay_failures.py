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
from sqlalchemy import event, select, text

from aris.core.access.models import User
from aris.core.audit import AuditEntry
from aris.core.relay.models import EventDelivery
from aris.core.time import utc
from tests.test_event_relay import command_state as command_state
from tests.test_event_relay import counts, seed
from tests.test_event_relay import durable_state as durable_state
from tests.test_event_relay import relay_state as relay_state


def test_relay_commit_boundary_lease_guard_rolls_back_business_and_inbox(relay_state):
    relay, sessions, ids, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    original = consumer.consume

    def delayed(session, envelope):
        original(session, envelope)

        def cross_lease(active):
            clock["now"] += timedelta(seconds=5)

        event.listen(session, "before_commit", cross_lease, once=True)

    consumer.consume = delayed
    result = relay.run_once()
    assert result.state == "lost_claim"
    assert counts(sessions) == (1, 1, 0)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert "event.delivery.processed" not in session.scalars(select(AuditEntry.action)).all()
    consumer.consume = original
    assert relay.run_once().state == "processed"
    assert counts(sessions) == (1, 1, 1)


@pytest.mark.parametrize("phase", ["claimed", "business_flushed", "committed"])
def test_killed_relay_fixture_recovers_without_partial_or_duplicate_consumer_work(
    relay_state, settings, phase
):
    relay, sessions, ids, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    program = textwrap.dedent("""
        import json, os, sys
        from datetime import datetime
        from aris.core.db import build_engine, session_factory
        from aris.core.relay.service import EventRelay
        from tests.test_event_relay import LIMITS, ProfileConsumer

        data = json.loads(os.environ['ARIS_FIXTURE_INPUT'])
        class Fixture(ProfileConsumer):
            def consume(self, session, envelope):
                super().consume(session, envelope)
                if data['phase'] == 'business_flushed':
                    session.flush()
                    print(json.dumps({'phase':'business_flushed'}), flush=True)
                    sys.stdin.read()

        engine = build_engine(os.environ['ARIS_FIXTURE_DATABASE_URL'])
        relay = EventRelay(session_factory(engine), [Fixture()], LIMITS,
            clock=lambda: datetime.fromisoformat(data['now']))
        claim = relay.claim()
        if data['phase'] == 'claimed':
            print(json.dumps({'phase':'claimed'}), flush=True)
            sys.stdin.read()
        result = relay.execute(claim)
        if data['phase'] == 'committed':
            assert result.state == 'processed'
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
            assert ready.select(timeout=10), "relay fixture did not reach its crash point"
        assert json.loads(child.stdout.readline()) == {"phase": phase}
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == (
            "Updated" if phase == "committed" else "Original A"
        )
        delivery = session.scalar(select(EventDelivery))
        assert delivery.state == ("processed" if phase == "committed" else "running")
        if phase != "committed":
            clock["now"] = utc(delivery.lease_expires_at)
    assert counts(sessions) == (1, 1, 1 if phase == "committed" else 0)
    if phase == "committed":
        assert relay.run_once() is None and consumer.calls == 0
    else:
        assert relay.run_once().state == "processed" and consumer.calls == 1
    assert counts(sessions) == (1, 1, 1)
    with sessions() as session:
        assert (
            list(session.scalars(select(AuditEntry.action))).count("event.delivery.processed") == 1
        )


def require_postgres(sessions):
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks, SKIP LOCKED and transaction budgets")


def test_postgres_concurrent_relays_fan_out_each_event_once(relay_state):
    relay, sessions, _, consumer, _, _ = relay_state
    require_postgres(sessions)
    for _ in range(3):
        seed(relay_state)
    barrier = Barrier(2)

    def publish(index):
        barrier.wait(timeout=5)
        return relay.publish_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sum(item.published for item in results) == 3
    assert sum(item.deliveries_queued for item in results) == 3
    assert counts(sessions) == (3, 3, 0) and consumer.calls == 0


def test_postgres_concurrent_consumers_commit_one_business_and_inbox(relay_state):
    relay, sessions, _, consumer, _, _ = relay_state
    require_postgres(sessions)
    seed(relay_state)
    relay.publish_once()
    barrier = Barrier(2)

    def work(index):
        barrier.wait(timeout=5)
        return relay.run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, range(2)))
    assert len([item for item in results if item is not None]) == 1
    assert next(item for item in results if item is not None).state == "processed"
    assert consumer.calls == 1 and counts(sessions) == (1, 1, 1)


def test_postgres_skip_locked_delivery_can_process_another_event(relay_state):
    relay, sessions, _, consumer, _, _ = relay_state
    require_postgres(sessions)
    seed(relay_state)
    seed(relay_state)
    relay.publish_once()
    with sessions.begin() as lock:
        held = lock.scalar(
            select(EventDelivery).order_by(EventDelivery.id).limit(1).with_for_update()
        )
        result = relay.run_once()
        assert result.state == "processed" and str(result.delivery_id) != held.id
    assert relay.run_once().state == "processed"
    assert consumer.calls == 2 and counts(sessions) == (2, 2, 2)


def test_postgres_business_lock_timeout_is_rolled_back_then_retried(relay_state):
    relay, sessions, ids, consumer, clock, _ = relay_state
    require_postgres(sessions)
    seed(relay_state)
    relay.publish_once()
    with sessions.begin() as lock:
        lock.execute(
            text("SELECT id FROM users WHERE id=:id FOR UPDATE"), {"id": ids["alice"]}
        ).one()
        started = time.monotonic()
        result = relay.run_once()
        assert result.state == "retry" and result.code == "RETRYABLE_DATABASE_ERROR"
        assert time.monotonic() - started < 2
        assert counts(sessions) == (1, 1, 0)
    clock["now"] += timedelta(seconds=1)
    assert relay.run_once().state == "processed" and consumer.calls == 2
    assert counts(sessions) == (1, 1, 1)
