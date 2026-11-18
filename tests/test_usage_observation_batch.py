import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, insert, select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_schemas import UsageObservationBatchQuery, UsageObservationQuery
from aris.modules.vega.usage_service import MAX_BATCH_SEQUENCE_SPAN
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import query, service
from tests.test_resource_telemetry import put, resource_batch


def batch(*queries):
    return UsageObservationBatchQuery(queries=queries)


def contiguous(*queries, cutoff=None):
    return UsageObservationBatchQuery(
        queries=queries, contiguous_source_cutoff=cutoff or datetime.now(UTC)
    )


def test_complete_span_requires_the_middle_observation_even_when_endpoints_match(client):
    a, b, c = (resource_batch(seq=value) for value in (1, 2, 3))
    for payload in (a, b, c):
        assert put(client, payload).status_code == 200
    observations = service(client)
    assert len(observations.observations(batch(query(a), query(c)))) == 2
    with pytest.raises(DomainError, match="incomplete or invalid"):
        observations.observations(contiguous(query(a), query(c)))
    wanted = (query(c), query(a), query(b), query(c))
    assert observations.observations(contiguous(*wanted)) == observations.observations(
        batch(*wanted)
    )


def test_unfilled_sequence_cannot_be_assumed_heartbeat_and_late_fill_cannot_change_old_cutoff(
    client,
):
    a, c = resource_batch(seq=1), resource_batch(seq=3)
    assert put(client, a).status_code == put(client, c).status_code == 200
    observations = service(client)
    old = contiguous(query(a), query(c))
    with pytest.raises(DomainError):
        observations.observations(old)
    assert beat(client, heartbeat(seq=2)).status_code == 200
    with pytest.raises(DomainError):
        observations.observations(old)
    assert observations.observations(contiguous(query(a), query(c))) == observations.observations(
        batch(query(a), query(c))
    )


@pytest.mark.parametrize("column", ["observed_at", "received_at"])
def test_intervening_heartbeat_must_be_available_at_cutoff(client, column):
    a, h, b = resource_batch(seq=1), heartbeat(seq=2), resource_batch(seq=3)
    assert (
        put(client, a).status_code
        == beat(client, h).status_code
        == put(client, b).status_code
        == 200
    )
    observations = service(client)
    wanted = contiguous(query(a), query(b))
    with observations.sessions.begin() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 2))
        setattr(row, column, wanted.contiguous_source_cutoff + timedelta(seconds=1))
        if column == "observed_at":
            row.payload = {
                **row.payload,
                "observed_at": row.observed_at.isoformat(),
                "sent_at": row.observed_at.isoformat(),
            }
            row.fingerprint = hashlib.sha256(
                json.dumps(row.payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
    with pytest.raises(DomainError):
        observations.observations(wanted)


@pytest.mark.parametrize("damage", ["fingerprint", "message_kind"])
def test_continuity_checks_heartbeat_originals_not_only_envelope_counts(client, damage):
    a, h, b = resource_batch(seq=1), heartbeat(seq=2), resource_batch(seq=3)
    assert (
        put(client, a).status_code
        == beat(client, h).status_code
        == put(client, b).status_code
        == 200
    )
    observations = service(client)
    with observations.sessions.begin() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 2))
        setattr(row, damage, "f" * 64 if damage == "fingerprint" else "telemetry")
    with pytest.raises(DomainError):
        observations.observations(contiguous(query(a), query(b)))


def test_complete_span_is_bounded_before_body_read_and_counts_heartbeat_bytes(client, monkeypatch):
    a, h, b = resource_batch(seq=1), heartbeat(seq=2), resource_batch(seq=3)
    assert (
        put(client, a).status_code
        == beat(client, h).status_code
        == put(client, b).status_code
        == 200
    )
    observations = service(client)
    far = query(b).model_copy(update={"expected_seq": MAX_BATCH_SEQUENCE_SPAN + 1})
    with pytest.raises(DomainError) as error:
        observations.observations(contiguous(query(a), far))
    assert error.value.code == "USAGE_SOURCE_TOO_LARGE"
    with observations.sessions() as session:
        sizes = VegaRepository(session).usage_message_metadata(
            "agent-a", (a["message_id"], b["message_id"])
        )
    monkeypatch.setattr(
        "aris.modules.vega.usage_service.MAX_BATCH_SOURCE_CHARACTERS", sum(row[2] for row in sizes)
    )

    def reject_body(*args):
        raise AssertionError("all source sizes must be checked before reading bodies")

    monkeypatch.setattr(VegaRepository, "usage_messages", reject_body)
    with pytest.raises(DomainError) as error:
        observations.observations(contiguous(query(a), query(b)))
    assert error.value.code == "USAGE_SOURCE_TOO_LARGE"


def test_exact_batch_preserves_order_duplicates_and_none_without_current_state_inference(client):
    a, b, c = resource_batch(seq=1), heartbeat(seq=2), resource_batch(seq=3)
    assert (
        put(client, a).status_code
        == beat(client, b).status_code
        == put(client, c).status_code
        == 200
    )
    observations = service(client)
    missing = query(c).model_copy(update={"message_id": uuid4(), "expected_seq": 4})
    wanted = (query(c), query(a), query(c), query(b), missing)
    expected = tuple(observations.observation(item) for item in wanted)
    assert observations.observations(batch(*wanted)) == expected
    assert expected[0] == expected[2] and expected[-2:] == (None, None)


def test_batch_contract_rejects_ambiguous_binding_and_references_without_database():
    q = UsageObservationQuery(
        agent_id="agent-a", node_id="node-a", message_id=uuid4(), expected_seq=1
    )
    for values in (
        (),
        (q,) * 1025,
        (q, q.model_copy(update={"node_id": "node-b"})),
        (q, q.model_copy(update={"agent_id": "agent-b"})),
        (q, q.model_copy(update={"expected_seq": 2})),
        (q, q.model_copy(update={"message_id": uuid4()})),
    ):
        with pytest.raises(ValidationError):
            batch(*values)


def test_actual_node_and_sequence_binding_is_checked_for_each_batch_member(client):
    payload = resource_batch(seq=1)
    assert put(client, payload).status_code == 200
    for changed in ({"node_id": "node-b"}, {"expected_seq": 2}):
        with pytest.raises(DomainError) as error:
            service(client).observations(batch(query(payload).model_copy(update=changed)))
        assert error.value.code == "CONFLICT"


def test_corrupted_one_of_many_sources_fails_the_entire_batch(client):
    a, b = resource_batch(seq=1), resource_batch(seq=2)
    assert put(client, a).status_code == put(client, b).status_code == 200
    observations = service(client)
    with observations.sessions.begin() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 2))
        row.fingerprint = "f" * 64
    with pytest.raises(DomainError) as error:
        observations.observations(batch(query(a), query(b)))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert observations.observation(query(a)) is not None


def test_source_size_is_checked_before_body_load_not_after_a_partial_answer(client, monkeypatch):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    monkeypatch.setattr("aris.modules.vega.usage_service.MAX_BATCH_SOURCE_CHARACTERS", 1)
    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DomainError) as error:
            service(client).observations(batch(query(payload)))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert error.value.code == "USAGE_SOURCE_TOO_LARGE"
    assert not any(item.startswith("SELECT agent_messages.agent_id") for item in statements)


def test_dirty_orm_and_pending_owner_work_cannot_replace_or_flush_original_sources(client):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    observations = service(client)
    wanted = contiguous(query(payload))
    original = observations.observations(wanted)
    with observations.sessions() as session:
        row = session.scalar(select(AgentMessage))
        row.payload = {"pending": "must not replace original body"}

        def reject(*args):
            raise AssertionError("batch lookup must not flush caller changes")

        event.listen(session, "before_flush", reject)
        try:
            assert observations.observations_in_transaction(session, wanted) == original
        finally:
            event.remove(session, "before_flush", reject)


def test_new_arrival_after_metadata_does_not_bypass_this_rounds_source_budget(client, monkeypatch):
    first, incoming = resource_batch(seq=1), resource_batch(seq=2)
    assert put(client, first).status_code == 200
    observations = service(client)
    original = VegaRepository.usage_message_metadata
    called = []

    def arrived(repo, agent_id, message_ids):
        rows = original(repo, agent_id, message_ids)
        if not called:
            called.append(True)
            assert put(client, incoming).status_code == 200
        return rows

    monkeypatch.setattr(VegaRepository, "usage_message_metadata", arrived)
    wanted = batch(query(incoming))
    assert observations.observations(wanted) == (None,)
    assert observations.observations(wanted)[0] is not None


def bulk_sources(client, count):
    first = resource_batch(seq=1)
    assert put(client, first).status_code == 200
    queries = [query(first)]
    rows = []
    for seq in range(2, count + 1):
        value = ResourceTelemetryBatch.model_validate(resource_batch(seq=seq))
        payload = value.model_dump(mode="json")
        rows.append(
            {
                "id": str(uuid4()),
                "agent_id": value.agent_id,
                "node_id": value.node_id,
                "message_id": str(value.message_id),
                "seq": seq,
                "message_kind": "telemetry",
                "observed_at": value.observed_at,
                "received_at": datetime.now(UTC),
                "fingerprint": hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "applied_to_current": False,
                "payload": payload,
            }
        )
        queries.append(
            UsageObservationQuery(
                agent_id=value.agent_id,
                node_id=value.node_id,
                message_id=value.message_id,
                expected_seq=seq,
            )
        )
    # The ingest path is tested separately; this read-contract fixture populates
    # valid immutable original rows without 1023 repeated HTTP setup calls.
    with Session(client.app.state.engine) as session, session.begin():
        session.execute(insert(AgentMessage), rows)
    return tuple(queries)


def test_1024_sources_need_16_queries_with_or_without_continuity_and_keep_every_binding(client):
    wanted = bulk_sources(client, 1024)
    observations = service(client)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if "agent_messages" in statement:
            statements.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        exact = observations.observations(batch(*wanted))
        assert len(statements) == 16
        statements.clear()
        result = observations.observations(contiguous(*wanted))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(result) == 1024 and len(statements) == 16
    assert result == exact
    assert all(statement.startswith("SELECT") for statement in statements)
    assert tuple(item.seq for item in result) == tuple(range(1, 1025))
    assert tuple(item.message_id for item in result) == tuple(item.message_id for item in wanted)


def test_1024_exact_duplicates_load_only_one_original_body(client):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    observations = service(client)
    result = observations.observations(batch(*((query(payload),) * 1024)))
    assert len(result) == 1024 and all(item == result[0] for item in result)


def test_postgresql_batch_is_read_only_and_preserves_owner_transaction_budget(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL batch read-only and owner budget")
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    observations = service(client)
    with observations.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2s'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        before = (
            session.scalar(text("SHOW transaction_timeout")),
            session.scalar(text("SHOW statement_timeout")),
        )
        assert (
            observations.observations_in_transaction(session, batch(query(payload)))[0] is not None
        )
        assert (
            observations.observations_in_transaction(session, contiguous(query(payload)))[0]
            is not None
        )
        assert before == (
            session.scalar(text("SHOW transaction_timeout")),
            session.scalar(text("SHOW statement_timeout")),
        )
