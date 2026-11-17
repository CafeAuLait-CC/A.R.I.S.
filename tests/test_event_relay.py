import json
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import LockNotAvailable
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from aris.core.access.models import Permission, RoleBinding, RolePermission, User
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import ActorContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.relay.commands import ReplayEventCommand
from aris.core.relay.models import ConsumerInbox, EventDelivery, EventPublication
from aris.core.relay.schemas import ConsumerDefinition, EventSelector, RelayLimits
from aris.core.relay.service import DatabaseEventConsumer, EventRelay
from aris.core.time import utc
from tests.test_command_intake import KEY
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state
from tests.test_commands import context

LIMITS = RelayLimits(
    lock_timeout_ms=200,
    statement_timeout_ms=500,
    transaction_timeout_ms=1000,
    lease_seconds=5,
    max_attempts=3,
    retry_base_seconds=1,
    batch_size=10,
)


class ProfileConsumer(DatabaseEventConsumer):
    definition = ConsumerDefinition(
        consumer_id="fixture.profile",
        revision="1",
        events=(EventSelector(event_type="fixture.rename", event_version=1),),
    )

    def __init__(self):
        self.calls = 0
        self.failure = None

    def consume(self, session, envelope):
        self.calls += 1
        user = session.get(User, envelope.payload["user_id"])
        user.display_name = envelope.payload["name"]
        if self.failure:
            raise self.failure


@pytest.fixture
def relay_state(durable_state):
    intake, sessions, ids, _, clock = durable_state
    consumer = ProfileConsumer()
    relay = EventRelay(sessions, [consumer], LIMITS, clock=lambda: clock["now"])
    return relay, sessions, ids, consumer, clock, intake


def seed(state, *, name="Updated", event_type="fixture.rename", version=1):
    _, sessions, ids, _, clock, _ = state
    event_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=event_type,
                event_version=version,
                aggregate_type="fixture",
                aggregate_id=ids["alice"],
                aggregate_version=1,
                occurred_at=clock["now"],
                recorded_at=clock["now"],
                actor="fixture",
                correlation_id=str(uuid4()),
                causation_id=str(uuid4()),
                payload={"user_id": ids["alice"], "name": name},
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=clock["now"], published_at=None))
    return event_id


def counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (EventPublication, EventDelivery, ConsumerInbox)
        )


def test_publication_is_durable_fanout_not_a_claim_of_consumer_delivery(relay_state):
    relay, sessions, ids, consumer, _, _ = relay_state
    event_id = seed(relay_state)
    result = relay.publish_once()
    assert result.published == result.deliveries_queued == 1
    assert counts(sessions) == (1, 1, 0) and consumer.calls == 0
    with sessions() as session:
        assert session.get(OutboxEntry, event_id).published_at is not None
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert session.scalar(select(EventDelivery)).state == "queued"
    assert relay.publish_once().published == 0
    assert relay.run_once().state == "processed"
    assert relay.run_once() is None
    assert counts(sessions) == (1, 1, 1) and consumer.calls == 1
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Updated"
        actions = list(session.scalars(select(AuditEntry.action)))
        assert actions.count("event.delivery.processed") == 1


def test_only_registered_exact_event_versions_publish_and_empty_registry_is_inert(relay_state):
    relay, sessions, _, consumer, _, _ = relay_state
    known = seed(relay_state)
    unknown = seed(relay_state, event_type="future.event")
    unsupported = seed(relay_state, version=2)
    disabled = EventRelay(sessions, (), LIMITS)
    assert disabled.publish_once().published == 0 and disabled.run_once() is None
    assert counts(sessions) == (0, 0, 0)
    assert relay.publish_once().published == 1
    with sessions() as session:
        assert session.get(OutboxEntry, known).published_at is not None
        assert all(
            session.get(OutboxEntry, item).published_at is None for item in (unknown, unsupported)
        )
    assert consumer.calls == 0


def test_multiple_subscribers_get_independent_unique_delivery_and_inbox(relay_state):
    relay, sessions, _, first, clock, _ = relay_state
    second = ProfileConsumer()
    second.definition = first.definition.model_copy(update={"consumer_id": "fixture.second"})
    relay = EventRelay(sessions, [first, second], LIMITS, clock=lambda: clock["now"])
    seed(relay_state)
    assert relay.publish_once().deliveries_queued == 2
    assert relay.run_once().state == relay.run_once().state == "processed"
    assert first.calls == second.calls == 1 and counts(sessions) == (1, 2, 2)
    with pytest.raises(ValueError, match="duplicate consumer"):
        EventRelay(sessions, [first, first], LIMITS)


def test_adding_a_subscriber_does_not_implicitly_replay_old_publications(relay_state):
    relay, sessions, _, first, clock, _ = relay_state
    seed(relay_state)
    assert relay.publish_once().published == 1
    second = ProfileConsumer()
    second.definition = first.definition.model_copy(update={"consumer_id": "fixture.new"})
    updated = EventRelay(sessions, [first, second], LIMITS, clock=lambda: clock["now"])
    assert updated.publish_once().published == 0
    assert updated.run_once().state == "processed"
    assert updated.run_once() is None and second.calls == 0
    assert counts(sessions) == (1, 1, 1)


def test_publication_failure_rolls_back_all_deliveries_and_published_marker(relay_state):
    relay, sessions, _, _, _, _ = relay_state
    event_id = seed(relay_state)
    original = relay._audit

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fixture-publication-failed")

    relay._audit = fail
    with pytest.raises(RuntimeError):
        relay.publish_once()
    assert counts(sessions) == (0, 0, 0)
    with sessions() as session:
        assert session.get(OutboxEntry, event_id).published_at is None
    relay._audit = original
    assert relay.publish_once().published == 1


@pytest.mark.parametrize("failure", ["permanent", "retryable"])
def test_failed_consumer_has_no_partial_business_or_inbox_and_preserves_sanitized_error(
    relay_state, failure
):
    relay, sessions, ids, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    consumer.failure = (
        OperationalError("fixture", None, LockNotAvailable("private-error"))
        if failure == "retryable"
        else RuntimeError("private-error")
    )
    result = relay.run_once()
    assert result.state == ("retry" if failure == "retryable" else "dead")
    assert counts(sessions) == (1, 1, 0)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        audit = list(session.scalars(select(AuditEntry)))
        assert "private-error" not in json.dumps([item.changes for item in audit])
        assert "event.delivery.processed" not in [item.action for item in audit]
    if failure == "retryable":
        assert relay.run_once() is None
        clock["now"] += timedelta(seconds=1)
        consumer.failure = None
        assert relay.run_once().state == "processed"
        assert counts(sessions) == (1, 1, 1)


def test_retry_attempts_stop_in_dead_letter_without_silently_discarding(relay_state):
    relay, sessions, ids, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    consumer.failure = OperationalError("fixture", None, LockNotAvailable("fixture"))
    for attempt in range(1, 4):
        result = relay.run_once()
        assert result.state == ("dead" if attempt == 3 else "retry")
        with sessions() as session:
            delivery = session.scalar(select(EventDelivery))
            assert delivery.attempts == attempt
            clock["now"] = utc(delivery.available_at)
    assert relay.run_once() is None and consumer.calls == 3
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert session.scalar(select(EventDelivery)).state == "dead"


@pytest.mark.parametrize("altered", ["event", "definition", "invalid_contract"])
def test_changed_or_invalid_event_and_changed_consumer_cannot_execute(relay_state, altered):
    relay, sessions, ids, consumer, clock, _ = relay_state
    event_id = seed(relay_state)
    if altered == "invalid_contract":
        with sessions.begin() as session:
            session.get(DomainEvent, event_id).causation_id = "invalid-id"
    relay.publish_once()
    if altered == "event":
        with sessions.begin() as session:
            session.get(DomainEvent, event_id).payload = {"different": "content"}
    elif altered == "definition":
        consumer.definition = consumer.definition.model_copy(update={"revision": "2"})
        relay = EventRelay(sessions, [consumer], LIMITS, clock=lambda: clock["now"])
    result = relay.run_once()
    assert result.state == "dead"
    assert (
        result.code
        == {
            "event": "EVENT_CHANGED",
            "definition": "CONSUMER_CHANGED",
            "invalid_contract": "EVENT_INVALID",
        }[altered]
    )
    assert consumer.calls == 0 and counts(sessions) == (1, 1, 0)


def test_expired_lease_can_be_taken_over_but_old_claim_cannot_commit(relay_state):
    relay, sessions, _, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    first = relay.claim()
    assert relay.claim() is None
    clock["now"] = first.lease_expires_at
    replacement = relay.claim()
    assert replacement.attempt == 2 and replacement.lease_token != first.lease_token
    assert relay.execute(first).state == "lost_claim" and consumer.calls == 0
    assert relay.execute(replacement).state == "processed" and consumer.calls == 1
    with sessions() as session:
        completed_at = session.scalar(select(EventDelivery.completed_at))
    clock["now"] += timedelta(seconds=1)
    assert relay.execute(first).state == "processed"
    with sessions() as session:
        assert session.scalar(select(EventDelivery.completed_at)) == completed_at
    assert consumer.calls == 1


@pytest.mark.parametrize("bad", ["commit", "nested_commit", "nested_failure", "lease_expiry"])
def test_consumer_cannot_commit_partial_work_or_outlive_its_lease(relay_state, bad):
    relay, sessions, ids, consumer, clock, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    original = consumer.consume

    def invalid(session, envelope):
        if bad in {"nested_commit", "nested_failure"}:
            with session.begin_nested():
                original(session, envelope)
                if bad == "nested_commit":
                    session.commit()
            if bad == "nested_failure":
                raise RuntimeError("failure-after-releasing-inner-savepoint")
        else:
            original(session, envelope)
            if bad == "commit":
                session.commit()
            else:
                clock["now"] += timedelta(seconds=5)

    consumer.consume = invalid
    result = relay.run_once()
    assert result.state == ("lost_claim" if bad == "lease_expiry" else "dead")
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert session.scalar(select(func.count()).select_from(ConsumerInbox)) == 0


def test_successful_consumer_can_use_nested_savepoints(relay_state):
    relay, sessions, ids, consumer, _, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    original = consumer.consume

    def nested(session, envelope):
        with session.begin_nested():
            original(session, envelope)

    consumer.consume = nested
    assert relay.run_once().state == "processed"
    assert counts(sessions) == (1, 1, 1)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Updated"


def test_inbox_suppresses_an_already_processed_event_if_delivery_is_requeued(relay_state):
    relay, sessions, _, consumer, _, _ = relay_state
    seed(relay_state)
    relay.publish_once()
    result = relay.run_once()
    assert result.state == "processed"
    # A fixture deliberately models a duplicate delivery, without deleting Inbox.
    with sessions.begin() as session:
        delivery = session.get(EventDelivery, str(result.delivery_id))
        delivery.state, delivery.completed_at = "queued", None
    assert relay.run_once().state == "processed"
    assert consumer.calls == 1 and counts(sessions) == (1, 1, 1)


def test_confirmed_privileged_replay_reuses_identity_and_preserves_failure_audit(relay_state):
    relay, sessions, ids, consumer, clock, _ = relay_state
    event_id = seed(relay_state)
    relay.publish_once()
    consumer.failure = RuntimeError("private-failed-consumer")
    failed = relay.run_once()
    assert failed.state == "dead"
    commands = CommandService(
        sessions, [ReplayEventCommand(relay)], confirmation_key=KEY, clock=lambda: clock["now"]
    )
    request = CommandRequest(
        command="events.replay",
        scope={"type": "global", "id": "*"},
        parameters={
            "delivery_id": str(failed.delivery_id),
            "expected_replay_count": 0,
            "reason": "Fixture recovery",
        },
    )
    with pytest.raises(DomainError) as denied:
        commands.execute(context(ids), request)
    assert denied.value.code == "PERMISSION_DENIED"
    with sessions.begin() as session:
        permission_id = str(uuid4())
        session.add(Permission(id=permission_id, name="events.replay"))
        session.flush()
        session.add(RolePermission(role_id=ids["role"], permission_id=permission_id))
    pending = commands.execute(context(ids), request)
    assert pending.state == "awaiting_confirmation"
    consumer.failure = None
    done = commands.confirm(
        ActorContext(actor_id=ids["alice"], request_id="replay-confirm"),
        ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
    )
    assert done.result["state"] == "queued" and done.result["replay_count"] == 1
    assert relay.run_once().state == "processed"
    assert counts(sessions) == (1, 1, 1)
    with sessions() as session:
        delivery = session.get(EventDelivery, str(failed.delivery_id))
        assert delivery.event_id == event_id and delivery.replay_count == 1
        actions = list(session.scalars(select(AuditEntry.action)))
        assert "event.delivery.dead" in actions and "event.delivery.replayed" in actions
    assert commands.confirm(
        ActorContext(actor_id=ids["alice"], request_id="replay-again"),
        ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
    ).duplicate
    assert relay.run_once() is None and consumer.calls == 2


@pytest.mark.parametrize(
    "changed", ["scope", "replay_count", "already_queued", "event", "definition"]
)
def test_replay_cannot_bypass_global_permission_or_changed_delivery_evidence(relay_state, changed):
    relay, sessions, ids, consumer, clock, _ = relay_state
    event_id = seed(relay_state)
    relay.publish_once()
    consumer.failure = RuntimeError("fixture-failure")
    dead = relay.run_once()
    with sessions.begin() as session:
        permission_id = str(uuid4())
        session.add(Permission(id=permission_id, name="events.replay"))
        session.flush()
        session.add(RolePermission(role_id=ids["role"], permission_id=permission_id))
        if changed == "scope":
            for binding in session.scalars(
                select(RoleBinding).where(RoleBinding.user_id == ids["alice"])
            ):
                binding.scope_type, binding.scope_id = "node", "fixture-node"
        elif changed == "replay_count":
            session.get(EventDelivery, str(dead.delivery_id)).replay_count = 1
        elif changed == "already_queued":
            delivery = session.get(EventDelivery, str(dead.delivery_id))
            delivery.state, delivery.completed_at = "queued", None
        elif changed == "event":
            session.get(DomainEvent, event_id).payload = {"changed": "fixture"}
    if changed == "definition":
        consumer.definition = consumer.definition.model_copy(update={"revision": "2"})
        relay = EventRelay(sessions, [consumer], LIMITS, clock=lambda: clock["now"])
    commands = CommandService(
        sessions, [ReplayEventCommand(relay)], confirmation_key=KEY, clock=lambda: clock["now"]
    )
    replay_request = CommandRequest(
        command="events.replay",
        scope={"type": "global", "id": "*"},
        parameters={
            "delivery_id": str(dead.delivery_id),
            "expected_replay_count": 0,
            "reason": "Fixture operator repair",
        },
    )
    if changed == "scope":
        with pytest.raises(DomainError) as denied:
            commands.execute(context(ids), replay_request)
        assert denied.value.code == "PERMISSION_DENIED"
        return
    pending = commands.execute(context(ids), replay_request)
    if changed == "definition":
        consumer.failure = None
        done = commands.confirm(
            ActorContext(actor_id=ids["alice"], request_id="replay"),
            ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
        )
        assert done.result["replay_count"] == 1
        assert relay.run_once().state == "processed" and consumer.calls == 2
    else:
        with pytest.raises(DomainError) as rejected:
            commands.confirm(
                ActorContext(actor_id=ids["alice"], request_id="replay"),
                ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
            )
        assert rejected.value.code == ("EVENT_CHANGED" if changed == "event" else "CONFLICT")
        assert counts(sessions) == (1, 1, 0)


def test_empty_relay_migration_round_trip_and_nonempty_history_guard(relay_state):
    relay, _, _, _, _, _ = relay_state
    config = Config("alembic.ini")
    command.downgrade(config, "0011")
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0012")
    seed(relay_state)
    relay.publish_once()
    with pytest.raises(RuntimeError, match="event relay history prevents downgrade"):
        command.downgrade(config, "0011")
    command.upgrade(config, "head")
    command.check(config)
