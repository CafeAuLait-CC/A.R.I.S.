from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import SCHEMA_REVISION
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.archive_guard_service import protect_archive_sources_in_transaction
from aris.modules.vega.models import (
    AgentArchiveState,
    AgentMessage,
    AgentMessageReceipt,
    NodeResource,
)
from aris.modules.vega.payload_retirement_schemas import (
    PayloadRetirementContext,
    PayloadRetirementRequest,
)
from aris.modules.vega.payload_retirement_service import VegaPayloadMaintenance
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery, UsageArchiveQuery
from tests.test_heartbeat import beat
from tests.test_resource_telemetry import put
from tests.test_trace_consumer import CONTEXT, configuration, consumer, rebuild, request, seed

__all__ = ["consumer"]
MAINTENANCE = PayloadRetirementContext(request_id="fixture-explicit-maintenance")
SOURCE = UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")


def prepare(state, first=1, last=2):
    with state.sessions() as db:
        reports = state.archive.reporting_page_in_transaction(
            db,
            UsageArchiveQuery(
                **SOURCE.model_dump(),
                after_seq=first - 1,
                through_seq=last,
            ),
        ).reports
    return PayloadRetirementRequest(
        id=uuid4(),
        source=SOURCE,
        before=datetime.now(UTC),
        dependency_fingerprint="c" * 64,
        reports=reports,
    )


def counts(state):
    with state.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(table))
            for table in (
                AgentMessage,
                AgentMessageReceipt,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
            )
        )


def test_atomic_payload_exit_replays_original_ack_and_resumes_at_the_recording_origin(
    consumer, monkeypatch
):
    state = consumer
    seed(state)
    state.config = configuration(initial_after_seq=2)
    state.service = rebuild(state)
    maintenance = VegaPayloadMaintenance(state.sessions)
    incoming = prepare(state)
    before = counts(state)
    engine = state.sessions.kw["bind"]
    with state.sessions() as db:
        originals = tuple(
            db.scalars(
                select(AgentMessage.payload).where(AgentMessage.seq <= 2).order_by(AgentMessage.seq)
            )
        )
        current = dict(db.scalar(select(NodeResource)).observation)

    def fail_delete(conn, cursor, statement, *args):
        if statement.lstrip().lower().startswith("delete from agent_messages"):
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Injected retirement interruption", 503)

    event.listen(engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(DomainError, match="Injected retirement interruption"):
            maintenance.retire(MAINTENANCE, incoming)
    finally:
        event.remove(engine, "before_cursor_execute", fail_delete)
    assert counts(state) == before
    result = maintenance.retire(MAINTENANCE, incoming)
    assert (result.first_seq, result.through_seq, result.retired_payloads) == (1, 2, 2)
    assert not result.duplicate
    assert counts(state) == tuple(
        value + change for value, change in zip(before, (-2, 2, 0, 0, 1), strict=True)
    )
    with state.sessions() as db:
        assert db.scalar(select(NodeResource)).observation == current
    after = counts(state)
    assert maintenance.retire(MAINTENANCE, incoming).duplicate
    altered = incoming.model_copy(update={"dependency_fingerprint": "d" * 64})
    with pytest.raises(DomainError, match="saved batch"):
        maintenance.retire(MAINTENANCE, altered)
    for original in originals:
        send = beat if original["type"] == "heartbeat" else put
        replayed = send(state.client, original)
        assert replayed.status_code == 200 and replayed.json()["duplicate"]
        conflict = send(state.client, {**original, "hostname": "different-original-host"})
        assert conflict.status_code == 409
        reused_sequence = send(state.client, {**original, "message_id": str(uuid4())})
        assert reused_sequence.status_code == 409
    assert counts(state) == after
    resumed = state.service.consume(CONTEXT, request(state))
    assert resumed.source_count == 4 and resumed.position.after_seq == 6
    assert (
        state.archive.page(
            UsageArchiveQuery(
                **SOURCE.model_dump(),
                after_seq=2,
                through_seq=6,
            )
        ).state
        == "range_end"
    )


def test_retirement_rejects_changed_sources_current_facts_and_nonprefix_ranges(consumer):
    state = consumer
    seed(state)
    maintenance = VegaPayloadMaintenance(state.sessions)
    before = counts(state)
    for first, last, message in ((2, 3, "oldest prefix"), (1, 5, "current fact")):
        with pytest.raises(DomainError, match=message):
            maintenance.retire(MAINTENANCE, prepare(state, first, last))
    incoming = prepare(state)
    with pytest.raises(DomainError, match="already be closed"):
        maintenance.retire(
            MAINTENANCE,
            incoming.model_copy(
                update={
                    "before": datetime.now(UTC) + timedelta(days=1),
                }
            ),
        )
    changed = incoming.reports[0].model_copy(update={"fingerprint": "e" * 64})
    with pytest.raises(DomainError, match="reports changed"):
        maintenance.retire(
            MAINTENANCE,
            incoming.model_copy(
                update={
                    "reports": (changed, *incoming.reports[1:]),
                }
            ),
        )
    assert counts(state) == before


@pytest.mark.parametrize("other_operation", ["same_retirement", "original_replay"])
def test_postgresql_retirement_serializes_with_replay_and_the_same_operation(
    consumer, other_operation
):
    state = consumer
    if state.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires real row locks and independent connections")
    seed(state)
    maintenance = VegaPayloadMaintenance(state.sessions)
    incoming = prepare(state)
    with state.sessions() as db:
        original = db.scalar(select(AgentMessage.payload).where(AgentMessage.seq == 1))
    before = counts(state)
    start = Barrier(2)

    def retire():
        start.wait(timeout=5)
        return maintenance.retire(MAINTENANCE, incoming)

    def other():
        start.wait(timeout=5)
        if other_operation == "same_retirement":
            return maintenance.retire(MAINTENANCE, incoming)
        return put(state.client, original)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(retire), pool.submit(other)
        a, b = left.result(timeout=10), right.result(timeout=10)
    if other_operation == "same_retirement":
        assert sorted((a.duplicate, b.duplicate)) == [False, True]
    else:
        assert b.status_code == 200 and b.json()["duplicate"]
    assert counts(state) == tuple(
        value + change for value, change in zip(before, (-2, 2, 0, 0, 1), strict=True)
    )


def test_postgresql_retirement_invalidates_old_rr_capture_and_protects_migration(
    consumer, settings, monkeypatch
):
    state = consumer
    if state.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL snapshot isolation and migrations")
    monkeypatch.setenv("ARIS_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0052")
    command.upgrade(cfg, "head")
    command.check(cfg)
    seed(state)
    maintenance = VegaPayloadMaintenance(state.sessions)
    incoming = prepare(state)
    with state.sessions() as stale:
        stale.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        assert stale.scalar(select(AgentArchiveState.retired_through_seq)) == 0
        maintenance.retire(MAINTENANCE, incoming)
        with pytest.raises(DBAPIError) as conflict:
            protect_archive_sources_in_transaction(stale, SOURCE, after_seq=0)
        assert conflict.value.orig.sqlstate == "40001"
        stale.rollback()
    with state.sessions() as fresh:
        with pytest.raises(DomainError, match="retired payloads"):
            protect_archive_sources_in_transaction(fresh, SOURCE, after_seq=0)
        assert protect_archive_sources_in_transaction(fresh, SOURCE, after_seq=2) == 2
    before = counts(state)
    with pytest.raises(DomainError, match="retired payloads"):
        state.service.consume(CONTEXT, request(state))
    assert counts(state) == before
    with pytest.raises(RuntimeError, match="retirement source boundaries"):
        command.downgrade(cfg, "0052")
    with state.sessions() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
