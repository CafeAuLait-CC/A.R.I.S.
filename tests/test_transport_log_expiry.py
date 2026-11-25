from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from aris.apps.composition.transport_log_expiry import TRANSPORT_TYPES, retire_transport_logs
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.log_expiry_schemas import LogScope
from aris.core.log_expiry_service import CoreLogMaintenance
from aris.core.relay.models import EventPublication
from aris.core.relay.schemas import ConsumerDefinition, EventSelector
from aris.core.relay.service import DatabaseEventConsumer, EventRelay
from aris.core.time import utc
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.payload_retirement_service import VegaPayloadMaintenance
from aris.modules.vega.repo import VegaRepository
from tests.test_event_relay import LIMITS
from tests.test_payload_retirement import MAINTENANCE, SOURCE, prepare
from tests.test_trace_consumer import consumer, seed

__all__ = ["consumer"]


def legacy_logs(state):
    """Explicit historical fixture: new polling no longer creates these copies."""
    seed(state)
    with state.sessions.begin() as db:
        for row in db.scalars(select(AgentMessage).where(AgentMessage.seq <= 2)):
            id, audit_id = str(uuid4()), str(uuid4())
            kind = "vega." + row.message_kind + ".accepted"
            request_id = "fixture-old-ack-" + str(row.seq)
            db.add(
                DomainEvent(
                    event_id=id,
                    event_type=kind,
                    event_version=1,
                    aggregate_type="node",
                    aggregate_id=row.node_id,
                    aggregate_version=row.seq,
                    occurred_at=utc(row.observed_at),
                    recorded_at=utc(row.received_at),
                    actor=row.agent_id,
                    correlation_id=request_id,
                    causation_id=row.message_id,
                    payload={
                        "message_id": row.message_id,
                        "applied_to_current": row.applied_to_current,
                    },
                )
            )
            db.flush()
            db.add(OutboxEntry(event_id=id, created_at=utc(row.received_at)))
            db.add(
                AuditEntry(
                    id=audit_id,
                    actor=row.agent_id,
                    action=kind,
                    target=row.node_id,
                    request_id=request_id,
                    recorded_at=utc(row.received_at),
                    changes={
                        "previous_seq": row.seq - 1,
                        "received_seq": row.seq,
                        "applied_to_current": row.applied_to_current,
                    },
                )
            )
    scope = LogScope(
        event_types=tuple(sorted(TRANSPORT_TYPES)),
        aggregate_type="node",
        aggregate_id=SOURCE.node_id,
        actor=SOURCE.agent_id,
        through_seq=2,
        before=datetime.now(UTC),
    )
    pairs = tuple(
        CoreLogMaintenance(state.sessions, allowed_types=TRANSPORT_TYPES).candidates(
            scope, audit_sequence_key="received_seq", maximum=1024
        )
    )
    assert len(pairs) == 2
    return scope, pairs


def log_counts(state):
    with state.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (DomainEvent, OutboxEntry, AuditEntry)
        )


def test_log_expiry_requires_retired_originals_and_is_atomic_and_idempotent(consumer):
    state = consumer
    scope, pairs = legacy_logs(state)
    before = log_counts(state)
    operation = uuid4()
    with pytest.raises(DomainError, match="have not left"):
        retire_transport_logs(state.sessions, MAINTENANCE, operation, SOURCE, scope, pairs)
    assert log_counts(state) == before
    VegaPayloadMaintenance(state.sessions).retire(MAINTENANCE, prepare(state))
    before = log_counts(state)
    engine = state.sessions.kw["bind"]

    def fail_after_outbox(conn, cursor, statement, *args):
        if statement.lower().startswith("delete from domain_events"):
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Injected log interruption", 503)

    event.listen(engine, "before_cursor_execute", fail_after_outbox)
    try:
        with pytest.raises(DomainError, match="Injected log interruption"):
            retire_transport_logs(state.sessions, MAINTENANCE, operation, SOURCE, scope, pairs)
    finally:
        event.remove(engine, "before_cursor_execute", fail_after_outbox)
    assert log_counts(state) == before
    wrong = uuid4()
    altered = pairs[0].model_copy(
        update={
            "event": pairs[0].event.model_copy(
                update={
                    "causation_id": wrong,
                    "payload": {"message_id": str(wrong), "applied_to_current": True},
                }
            )
        }
    )
    with pytest.raises(DomainError, match="original acknowledgment"):
        retire_transport_logs(
            state.sessions, MAINTENANCE, operation, SOURCE, scope, (altered, pairs[1])
        )
    result = retire_transport_logs(state.sessions, MAINTENANCE, operation, SOURCE, scope, pairs)
    assert (result.events, result.audits, result.duplicate) == (2, 2, False)
    assert log_counts(state) == (before[0] - 2, before[1] - 2, before[2] - 1)
    assert retire_transport_logs(
        state.sessions, MAINTENANCE, operation, SOURCE, scope, pairs
    ).duplicate
    with state.sessions() as db:
        for pair in pairs:
            original = VegaRepository(db).message(SOURCE.agent_id, str(pair.event.causation_id))
            assert original and original.applied_to_current


def test_log_expiry_keeps_publications_and_unknown_foreign_key_references(consumer):
    state = consumer
    scope, pairs = legacy_logs(state)
    VegaPayloadMaintenance(state.sessions).retire(MAINTENANCE, prepare(state))
    before = log_counts(state)
    with state.sessions.begin() as db:
        db.add(
            EventPublication(
                event_id=str(pairs[0].event.event_id),
                event_fingerprint="a" * 64,
                registry_fingerprint="b" * 64,
                subscriber_count=1,
                created_at=datetime.now(UTC),
            )
        )
    with pytest.raises(DomainError, match="referenced"):
        retire_transport_logs(state.sessions, MAINTENANCE, uuid4(), SOURCE, scope, pairs)
    assert log_counts(state) == before
    with state.sessions.begin() as db:
        db.delete(db.get(EventPublication, str(pairs[0].event.event_id)))
        db.execute(
            text(
                "CREATE TABLE fixture_unknown_log_ref "
                "(id varchar(36) PRIMARY KEY REFERENCES domain_events(event_id))"
            )
        )
        db.execute(
            text("INSERT INTO fixture_unknown_log_ref(id) VALUES(:id)"),
            {"id": str(pairs[0].event.event_id)},
        )
    with pytest.raises(DomainError, match="referenced") as failure:
        retire_transport_logs(state.sessions, MAINTENANCE, uuid4(), SOURCE, scope, pairs)
    assert failure.value.code == "CONFLICT" and log_counts(state) == before


def test_postgresql_log_expiry_serializes_with_an_unexpected_publisher(consumer):
    state = consumer
    if state.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires real Outbox row locks and independent transactions")
    scope, pairs = legacy_logs(state)
    VegaPayloadMaintenance(state.sessions).retire(MAINTENANCE, prepare(state))

    class UnexpectedConsumer(DatabaseEventConsumer):
        definition = ConsumerDefinition(
            consumer_id="fixture.unexpected",
            revision="1",
            events=tuple(
                EventSelector(event_type=name, event_version=1) for name in sorted(TRANSPORT_TYPES)
            ),
        )

        def consume(self, session, envelope):
            raise AssertionError("Publishing only; no delivery is authorized")

    relay = EventRelay(state.sessions, [UnexpectedConsumer()], LIMITS)
    barrier = Barrier(2)

    def expire():
        barrier.wait(timeout=5)
        try:
            return retire_transport_logs(state.sessions, MAINTENANCE, uuid4(), SOURCE, scope, pairs)
        except DomainError as failure:
            assert failure.code == "CONFLICT"
            return None

    def publish():
        barrier.wait(timeout=5)
        return relay.publish_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(expire), pool.submit(publish)
        expired, publication = left.result(timeout=10), right.result(timeout=10)
    with state.sessions() as db:
        remaining = db.scalar(select(func.count()).select_from(DomainEvent))
        references = db.scalar(select(func.count()).select_from(EventPublication))
    if expired is None:
        assert remaining == 2 and references == publication.published > 0
    else:
        assert expired.events == 2 and remaining == references == publication.published == 0
