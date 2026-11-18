import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

from aris.core.contracts import DomainError
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.schemas import Heartbeat
from aris.modules.vega.usage_archive_schemas import (
    ArchivedUsageItem,
    ArchivedUsageReference,
    UsageArchivePage,
    UsageArchiveQuery,
    archive_page_fingerprint,
)
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import projection
from tests.test_resource_telemetry import put, resource_batch


def query(**changes):
    return UsageArchiveQuery.model_validate(
        {"agent_id": "agent-a", "node_id": "node-a", "after_seq": 0, "through_seq": 5, **changes}
    )


def archive(client):
    return VegaUsageArchive(sessionmaker(client.app.state.engine, expire_on_commit=False))


def seed(client):
    payloads = []
    for seq in range(1, 6):
        payload = resource_batch(seq=seq) if seq % 2 else heartbeat(seq=seq)
        response = put(client, payload) if seq % 2 else beat(client, payload)
        assert response.status_code == 200
        payloads.append(payload)
    return payloads


def pure_page():
    fact = projection(resource_batch(seq=1))
    item = ArchivedUsageItem(
        reference=ArchivedUsageReference(
            agent_id=fact.agent_id,
            node_id=fact.node_id,
            message_id=fact.message_id,
            seq=fact.seq,
            kind="telemetry",
            fingerprint=fact.source_fingerprint,
            observed_at=fact.observed_at,
            received_at=fact.received_at,
        ),
        observation=fact,
    )
    page = UsageArchivePage.model_construct(
        query=query(through_seq=1),
        items=(item,),
        next_after_seq=1,
        state="range_end",
        read_at=datetime.now(UTC),
        fingerprint="0" * 64,
    )
    return UsageArchivePage.model_validate(
        {**page.model_dump(mode="json"), "fingerprint": archive_page_fingerprint(page)}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"after_seq": -1},
        {"after_seq": True},
        {"through_seq": 0},
        {"through_seq": 9223372036854775808},
        {"after_seq": 5},
        {"limit": 129},
        {"limit": "2"},
        {"complete": True},
    ],
)
def test_archive_range_is_explicit_bounded_and_cannot_supply_completeness(change):
    with pytest.raises(ValidationError):
        query(**change)


@pytest.mark.parametrize("change", ["cursor", "gap", "authority", "source", "heartbeat"])
def test_archive_page_cannot_misrepresent_its_prefix_or_observations(change):
    page = pure_page()
    raw = page.model_dump(mode="json")
    if change == "cursor":
        raw["next_after_seq"] = 2
    elif change == "gap":
        raw["state"], raw["missing_seq"] = "gap", 1
    elif change == "authority":
        raw["quota_usage_available"] = True
    elif change == "source":
        raw["items"][0]["reference"]["fingerprint"] = "c" * 64
    else:
        raw["items"][0]["reference"]["kind"] = "heartbeat"
    with pytest.raises(ValidationError):
        UsageArchivePage.model_validate(raw)


def test_source_pages_are_sequence_ordered_and_heartbeats_never_become_usage_samples(client):
    seed(client)
    service = archive(client)
    seen = []
    selected = query(limit=2)
    while True:
        page = service.page(selected)
        assert not page.quota_usage_available
        for item in page.items:
            seen.append(item.reference.seq)
            assert (item.observation is None) == (item.reference.kind == "heartbeat")
            assert "payload" not in item.model_dump() and "hostname" not in item.model_dump()
        if page.state == "range_end":
            break
        assert page.state == "page_end" and page.missing_seq is None
        selected = selected.model_copy(update={"after_seq": page.next_after_seq})
    assert seen == [1, 2, 3, 4, 5]
    first = service.page(query())
    second = service.page(query())
    assert first.fingerprint == second.fingerprint and first.read_at <= second.read_at


def test_high_sequence_and_later_arrival_do_not_advance_over_an_archive_gap(client):
    assert put(client, resource_batch(seq=3)).status_code == 200
    service = archive(client)
    empty = service.page(query(through_seq=3))
    assert empty.state == "gap" and empty.next_after_seq == 0 and empty.missing_seq == 1
    assert put(client, resource_batch(seq=1)).status_code == 200
    partial = service.page(query(through_seq=3))
    assert partial.state == "gap" and partial.next_after_seq == 1 and partial.missing_seq == 2
    assert [x.reference.seq for x in partial.items] == [1]
    assert service.page(query(after_seq=1, through_seq=3)).items == ()
    assert beat(client, heartbeat(seq=2)).status_code == 200
    complete = service.page(query(through_seq=3))
    assert complete.state == "range_end" and complete.next_after_seq == 3
    assert complete.items[0] == partial.items[0]
    assert service.page(query(through_seq=4)).missing_seq == 4


def test_source_binding_is_checked_without_exposing_a_route_or_querying_current_gpus(client):
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    for changes, code in (
        ({"node_id": "node-b"}, "CONFLICT"),
        ({"agent_id": "other"}, "NOT_FOUND"),
    ):
        with pytest.raises(DomainError) as error:
            service.page(query(**changes))
        assert error.value.code == code


@pytest.mark.parametrize("corruption", ["heartbeat_hash", "heartbeat_time", "telemetry_format"])
def test_bad_original_source_is_not_skipped_or_turned_into_idle(client, corruption):
    seed(client)
    service = archive(client)
    with service.sessions.begin() as session:
        seq = 1 if corruption == "telemetry_format" else 2
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == seq))
        if corruption == "heartbeat_hash":
            row.fingerprint = "f" * 64
        elif corruption == "heartbeat_time":
            row.observed_at += timedelta(seconds=1)
        else:
            value = copy.deepcopy(row.payload)
            value.pop("resources")
            row.payload = value
            row.fingerprint = hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
    with pytest.raises(DomainError) as error:
        service.page(query())
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_archive_does_not_autoflush_or_use_pending_orm_source_edits(client):
    seed(client)
    service = archive(client)
    before = service.page(query())
    with service.sessions() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 1))
        row.payload = {"uncommitted": "fixture must not replace stored evidence"}

        def reject(*args):
            raise AssertionError("archive read must never flush caller state")

        event.listen(session, "before_flush", reject)
        try:
            assert service.page_in_transaction(session, query()).fingerprint == before.fingerprint
        finally:
            event.remove(session, "before_flush", reject)


def test_source_budget_is_checked_before_loading_payloads_and_does_not_return_truncated_success(
    client, monkeypatch
):
    seed(client)
    service = archive(client)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    monkeypatch.setattr("aris.modules.vega.usage_archive_service.MAX_ARCHIVE_SOURCE_CHARACTERS", 1)
    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DomainError) as error:
            service.page(query())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert error.value.code == "USAGE_ARCHIVE_TOO_LARGE"
    assert not any(
        statement.startswith("SELECT agent_messages.agent_id") for statement in statements
    )


def postgres(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL archived source read isolation")


def test_postgresql_archive_is_read_only_with_constant_query_count(client):
    postgres(client)
    seed(client)
    service = archive(client)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with service.sessions() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            page = service.page_in_transaction(session, query())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert page.state == "range_end" and len(page.items) == 5 and len(statements) <= 5
    assert all(
        statement.startswith(("SELECT", "SET TRANSACTION READ ONLY")) for statement in statements
    )
    assert all(
        "gpu_devices" not in statement and "node_resources" not in statement
        for statement in statements
    )


def test_postgresql_uncommitted_gap_fill_is_not_a_consumed_source(client):
    postgres(client)
    assert put(client, resource_batch(seq=1)).status_code == 200
    assert put(client, resource_batch(seq=3)).status_code == 200
    service = archive(client)
    batch = Heartbeat.model_validate(heartbeat(seq=2))
    payload = batch.model_dump(mode="json")
    with service.sessions.begin() as writer:
        writer.add(
            AgentMessage(
                id=str(uuid4()),
                message_id=str(batch.message_id),
                agent_id=batch.agent_id,
                node_id=batch.node_id,
                seq=2,
                message_kind="heartbeat",
                fingerprint=hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                observed_at=batch.observed_at,
                received_at=datetime.now(UTC),
                applied_to_current=False,
                payload=payload,
            )
        )
        writer.flush()
        page = service.page(query(through_seq=3))
        assert page.state == "gap" and page.missing_seq == 2 and page.next_after_seq == 1
    assert service.page(query(through_seq=3)).state == "range_end"


def test_postgresql_archive_lock_timeout_is_retryable_without_cursor_advance(client):
    postgres(client)
    seed(client)
    service = archive(client)
    with service.sessions.begin() as locker:
        locker.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            service.page(query())
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert service.page(query()).next_after_seq == 5
