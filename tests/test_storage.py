from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from aris.core.db import Base, session_factory
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.storage import inspect_pending_events, inspect_storage
from aris.modules.vega.service import VegaService
from tests.test_heartbeat import beat, heartbeat
from tests.test_telemetry import batch, post


def pending_fixtures(client, *types):
    with Session(client.app.state.engine) as db, db.begin():
        for name in types:
            id = str(uuid4())
            now = datetime.now(UTC)
            db.add(
                DomainEvent(
                    event_id=id,
                    event_type=name,
                    event_version=1,
                    aggregate_type="fixture",
                    aggregate_id="fixture",
                    aggregate_version=1,
                    occurred_at=now,
                    recorded_at=now,
                    actor="fixture",
                    correlation_id=id,
                    causation_id=id,
                    payload={"fixture": True},
                )
            )
            db.flush()
            db.add(OutboxEntry(event_id=id, created_at=now))


def test_retention_preview_counts_streams_but_performs_no_writes(client, settings):
    assert post(client, batch()).status_code == 200
    assert beat(client, heartbeat(seq=2)).status_code == 200
    pending_fixtures(client, "fixture.first", "fixture.second")
    service = VegaService(session_factory(client.app.state.engine), settings)
    queries = []
    event.listen(
        client.app.state.engine,
        "before_cursor_execute",
        lambda conn, cursor, sql, *args: queries.append(sql),
    )
    future = datetime.now(UTC) + timedelta(minutes=1)
    result = service.storage(future)
    assert {item.message_kind: item.count for item in result.messages} == {
        "telemetry": 1,
        "heartbeat": 1,
    }
    assert all(item.before_cutoff == 1 for item in result.messages)
    assert not result.deletion_enabled
    assert any("deduplication" in blocker for blocker in result.blockers)
    assert all(item.before_cutoff is None for item in service.storage().messages)
    assert all(
        item.before_cutoff == 0
        for item in service.storage(datetime.now(UTC) - timedelta(days=1)).messages
    )
    inventory = inspect_pending_events(client.app.state.engine)
    assert inventory.total_pending == sum(row.pending_count for row in inventory.event_types) == 2
    assert len(inventory.event_types) == 2 and not inventory.truncated
    bounded = inspect_pending_events(client.app.state.engine, type_limit=1)
    assert bounded.total_pending == 2 and len(bounded.event_types) == 1 and bounded.truncated
    assert bounded.oldest_created_at == inventory.oldest_created_at
    assert not bounded.deletion_enabled and bounded.read_only
    assert not any(word in bounded.model_dump_json() for word in ("payload", "aggregate_id"))
    for invalid in (0, 65, True):
        with pytest.raises(ValueError, match="event type limit"):
            inspect_pending_events(client.app.state.engine, type_limit=invalid)
    offset = future.astimezone(timezone(timedelta(hours=8)))
    assert service.storage(offset).messages == result.messages
    with pytest.raises(ValueError, match="time zone"):
        service.storage(datetime(2026, 1, 1))
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql in queries)


def test_storage_reports_pending_outbox_without_claiming_delivery_or_fake_bytes(client):
    empty = inspect_pending_events(client.app.state.engine)
    assert empty.total_pending == 0 and empty.oldest_created_at is None
    assert empty.event_types == () and not empty.truncated
    assert post(client, batch()).status_code == 200
    pending_fixtures(client, "fixture.pending")
    result = inspect_storage(client.app.state.engine)
    assert result.pending_domain_events == 1
    assert result.oldest_pending_event_at.tzinfo is not None
    if client.app.state.engine.dialect.name == "sqlite":
        assert result.status == "unsupported"
        assert result.relation_bytes is None and result.database_bytes is None
        assert result.relations == []


def test_postgres_storage_sizes_are_scoped_to_current_test_schema_and_read_only(client):
    engine = client.app.state.engine
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL relation metadata")
    queries = []
    event.listen(
        engine, "before_cursor_execute", lambda conn, cursor, sql, *args: queries.append(sql)
    )
    result = inspect_storage(engine)
    assert result.status == "measured"
    assert {row.table_name for row in result.relations} == set(Base.metadata.tables) | {
        "alembic_version"
    }
    assert result.relation_bytes == sum(row.total_bytes for row in result.relations)
    assert result.database_bytes >= result.relation_bytes > 0
    assert result.rows_are_estimates
    assert all(row.estimated_live_rows >= 0 and row.index_bytes >= 0 for row in result.relations)
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql in queries)
