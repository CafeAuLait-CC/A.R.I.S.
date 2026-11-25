from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.modules.vega.models import (
    AgentArchiveState,
    AgentMessage,
    AgentMessageReceipt,
    AgentReceiptBlock,
)
from aris.modules.vega.payload_retirement_service import VegaPayloadMaintenance
from aris.modules.vega.receipt_block_codec import (
    OriginalReceipt,
    block_fingerprint,
    decode_receipts,
    encode_receipts,
)
from aris.modules.vega.receipt_block_repo import ReceiptBlockRepository
from aris.modules.vega.receipt_compaction_service import VegaReceiptMaintenance
from aris.modules.vega.repo import VegaRepository
from tests.test_heartbeat import beat
from tests.test_payload_retirement import MAINTENANCE, SOURCE, counts, prepare
from tests.test_resource_telemetry import put
from tests.test_trace_consumer import CONTEXT, configuration, consumer, rebuild, request, seed

__all__ = ["consumer"]


def test_receipt_block_is_bounded_lossless_and_checks_index_flags_and_scope():
    rows = tuple(
        OriginalReceipt(index + 1, str(uuid4()), f"{index:064x}", bool(index % 2))
        for index in range(128)
    )
    packed = encode_receipts(rows)
    assert len(packed) == 128 * 49
    fingerprint = block_fingerprint("agent-a", 1, 128, packed)
    ids = [row.message_id for row in rows]
    assert decode_receipts("agent-a", 1, 128, packed, ids, fingerprint) == rows
    for changes in (
        ("agent-b", 1, 128, packed, ids, fingerprint),
        ("agent-a", 1, 128, packed, ids[::-1], fingerprint),
        ("agent-a", 1, 128, packed[:-1], ids, fingerprint),
    ):
        with pytest.raises(ValueError):
            decode_receipts(*changes)
    badflag = packed[:48] + b"\x02" + packed[49:]
    with pytest.raises(ValueError, match="flag"):
        decode_receipts(
            "agent-a", 1, 128, badflag, ids, block_fingerprint("agent-a", 1, 128, badflag)
        )
    for invalid in (
        (),
        (*rows, replace(rows[-1], seq=129, message_id=str(uuid4()))),
        (rows[0], rows[2]),
        (rows[0], replace(rows[1], message_id=rows[0].message_id)),
    ):
        with pytest.raises(ValueError):
            encode_receipts(invalid)


def test_compaction_is_atomic_replays_old_acks_and_retirement_then_allows_next_prefix(consumer):
    state = consumer
    seed(state)
    state.config = configuration(initial_after_seq=4)
    state.service = rebuild(state)
    payloads = VegaPayloadMaintenance(state.sessions)
    service = VegaReceiptMaintenance(state.sessions)
    assert service.prepare(SOURCE, uuid4()) is None
    with state.sessions() as db:
        originals = tuple(
            db.scalars(
                select(AgentMessage.payload).where(AgentMessage.seq <= 4).order_by(AgentMessage.seq)
            )
        )
    first_retirement = prepare(state)
    payloads.retire(MAINTENANCE, first_retirement)
    incoming = service.prepare(SOURCE, uuid4())
    assert incoming is not None and (incoming.first_seq, incoming.through_seq) == (1, 2)
    before = counts(state)
    engine = state.sessions.kw["bind"]
    with state.sessions() as db:
        with pytest.raises(DBAPIError, match="receipts are immutable"):
            db.execute(text("DELETE FROM agent_message_receipts WHERE seq=1"))
        db.rollback()

    def interrupt(conn, cursor, statement, *args):
        if statement.lstrip().lower().startswith("delete from agent_message_receipts"):
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Injected compaction interruption", 503)

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(DomainError, match="Injected"):
            service.compact(MAINTENANCE, incoming)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    assert counts(state) == before
    with state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentReceiptBlock)) == 0
        assert db.get(AgentArchiveState, SOURCE.agent_id).receipts_compacted_through_seq == 0
    result = service.compact(MAINTENANCE, incoming)
    assert result.compacted_receipts == 2 and not result.duplicate
    assert service.compact(MAINTENANCE, incoming).duplicate
    assert payloads.retire(MAINTENANCE, first_retirement).duplicate
    with pytest.raises(DomainError, match="saved batch"):
        service.compact(MAINTENANCE, incoming.model_copy(update={"block_fingerprint": "a" * 64}))
    for original in originals[:2]:
        send = beat if original["type"] == "heartbeat" else put
        assert send(state.client, original).json()["duplicate"]
        assert send(state.client, {**original, "hostname": "different"}).status_code == 409
        assert send(state.client, {**original, "message_id": str(uuid4())}).status_code == 409
        assert send(state.client, {**original, "seq": 99}).status_code == 409
    second_retirement = prepare(state, 3, 4)
    payloads.retire(MAINTENANCE, second_retirement)
    second = service.prepare(SOURCE, uuid4())
    assert second is not None and second.first_seq == 3
    service.compact(MAINTENANCE, second)
    assert payloads.retire(MAINTENANCE, first_retirement).duplicate
    assert payloads.retire(MAINTENANCE, second_retirement).duplicate
    with state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AgentMessageReceipt)) == 0
        assert db.scalar(select(func.count()).select_from(AgentReceiptBlock)) == 2
        repo = ReceiptBlockRepository(db)
        assert len(repo.original(SOURCE.agent_id, 2, 3)) == 2
        assert repo.message("different-agent", originals[0]["message_id"]) is None
        assert VegaRepository(db).retired_sequence_count_before(SOURCE.agent_id, 5) == 4
        block = db.get(AgentReceiptBlock, str(incoming.id))
        block.fingerprint = "0" * 64
        with pytest.raises(DBAPIError, match="receipt blocks are immutable"):
            db.commit()
        db.rollback()
    assert put(state.client, originals[0]).json()["duplicate"]
    resumed = state.service.consume(CONTEXT, request(state))
    assert resumed.source_count == 2 and resumed.position.after_seq == 6


@pytest.mark.parametrize("operation", ["same_compaction", "old_ingest"])
def test_postgresql_compaction_serializes_replay_and_preserves_index_and_migration(
    consumer, settings, monkeypatch, operation
):
    state = consumer
    if state.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locks, GIN and schema migrations")
    monkeypatch.setenv("ARIS_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0053")
    command.upgrade(cfg, "head")
    command.check(cfg)
    seed(state)
    with state.sessions() as db:
        original = db.scalar(select(AgentMessage.payload).where(AgentMessage.seq == 1))
    payloads = VegaPayloadMaintenance(state.sessions)
    old_request = prepare(state)
    payloads.retire(MAINTENANCE, old_request)
    service = VegaReceiptMaintenance(state.sessions)
    incoming = service.prepare(SOURCE, uuid4())
    assert incoming is not None
    barrier = Barrier(2)

    def compact():
        barrier.wait(timeout=5)
        return service.compact(MAINTENANCE, incoming)

    def other():
        barrier.wait(timeout=5)
        return (
            service.compact(MAINTENANCE, incoming)
            if operation == "same_compaction"
            else put(state.client, original)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(compact), pool.submit(other)
        left, right = a.result(timeout=10), b.result(timeout=10)
    if operation == "same_compaction":
        assert sorted((left.duplicate, right.duplicate)) == [False, True]
    else:
        assert right.status_code == 200 and right.json()["duplicate"]
    assert payloads.retire(MAINTENANCE, old_request).duplicate
    with state.sessions() as db:
        db.execute(text("SET LOCAL enable_seqscan=off"))
        plan = db.execute(
            text(
                "EXPLAIN SELECT id FROM agent_receipt_blocks "
                "WHERE message_ids @> cast(:ids as jsonb)"
            ),
            {"ids": '["' + original["message_id"] + '"]'},
        ).all()
        assert "ix_agent_receipt_block_message_ids" in str(plan)
    with pytest.raises(RuntimeError, match="Cannot discard compacted"):
        command.downgrade(cfg, "0053")
    command.check(cfg)
