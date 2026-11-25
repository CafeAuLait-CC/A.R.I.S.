"""Queries for hot acknowledgments and their lossless compacted prefix."""

from sqlalchemy import BigInteger, LargeBinary, and_, cast, delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from aris.core.contracts import DomainError
from aris.modules.vega.models import (
    AgentArchiveState,
    AgentMessage,
    AgentMessageReceipt,
    AgentReceiptBlock,
)
from aris.modules.vega.receipt_block_codec import OriginalReceipt, decode_receipts


def decoded(block: AgentReceiptBlock) -> tuple[OriginalReceipt, ...]:
    try:
        return decode_receipts(
            block.agent_id,
            block.first_seq,
            block.through_seq,
            block.packed_receipts,
            block.message_ids,
            block.fingerprint,
        )
    except ValueError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Stored message receipt block is invalid", 503
        ) from None


class ReceiptBlockRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def message(self, agent_id: str, message_id: str) -> OriginalReceipt | None:
        block = AgentReceiptBlock
        contains: ColumnElement[bool]
        if self.session.get_bind().dialect.name == "postgresql":
            contains = block.message_ids.op("@>")(literal([message_id], type_=JSONB()))
        else:
            values = func.json_each(block.message_ids).table_valued("value")
            contains = select(values.c.value).where(values.c.value == message_id).exists()
        queries = [
            select(
                row.seq,
                row.fingerprint,
                row.applied_to_current,
                cast(literal(None), LargeBinary()),
                literal(None, type_=block.message_ids.type),
                cast(literal(None), BigInteger),
            ).where(row.agent_id == agent_id, row.message_id == message_id)
            for row in (AgentMessage, AgentMessageReceipt)
        ]
        statement = (
            queries[0]
            .union_all(
                queries[1],
                select(
                    block.first_seq,
                    block.fingerprint,
                    literal(False),
                    block.packed_receipts,
                    block.message_ids,
                    block.through_seq,
                ).where(block.agent_id == agent_id, contains),
            )
            .limit(4)
        )
        found: set[OriginalReceipt] = set()
        for seq, fingerprint, applied, packed, ids, last in self.session.execute(statement):
            if packed is None:
                found.add(OriginalReceipt(seq, message_id, fingerprint, applied))
            else:
                try:
                    rows = decode_receipts(agent_id, seq, last, packed, ids, fingerprint)
                except ValueError:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Stored message receipt block is invalid", 503
                    ) from None
                matches = tuple(row for row in rows if row.message_id == message_id)
                if len(matches) != 1:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Stored message receipt index differs", 503
                    )
                found.add(matches[0])
        if len(found) > 1:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Stored message receipts disagree", 503)
        return next(iter(found)) if found else None

    def hot(self, agent_id: str, first: int, last: int) -> tuple[OriginalReceipt, ...]:
        row = AgentMessageReceipt
        return tuple(
            OriginalReceipt(*value)
            for value in self.session.execute(
                select(row.seq, row.message_id, row.fingerprint, row.applied_to_current)
                .where(row.agent_id == agent_id, row.seq.between(first, last))
                .order_by(row.seq)
            )
        )

    def original(self, agent_id: str, first: int, last: int) -> tuple[OriginalReceipt, ...]:
        if not 1 <= last - first + 1 <= 128:
            raise ValueError("Original receipt lookup must be bounded")
        rows = list(self.hot(agent_id, first, last))
        block = AgentReceiptBlock
        blocks = self.session.scalars(
            select(block)
            .where(block.agent_id == agent_id, block.first_seq <= last, block.through_seq >= first)
            .order_by(block.first_seq)
            .limit(129)
        ).all()
        if len(blocks) > 128:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored message receipt ranges overlap", 503
            )
        rows.extend(row for item in blocks for row in decoded(item) if first <= row.seq <= last)
        if len({row.seq for row in rows}) != len(rows):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored message receipt ranges overlap", 503
            )
        return tuple(sorted(rows, key=lambda row: row.seq))

    def compacted_through(self, agent_id: str) -> int:
        return (
            self.session.scalar(
                select(AgentArchiveState.receipts_compacted_through_seq).where(
                    AgentArchiveState.agent_id == agent_id
                )
            )
            or 0
        )

    def replace(self, block: AgentReceiptBlock, previous: int) -> None:
        self.session.add(block)
        self.session.flush()
        hot = AgentMessageReceipt
        removed = self.session.scalars(
            delete(hot)
            .where(
                hot.agent_id == block.agent_id, hot.seq.between(block.first_seq, block.through_seq)
            )
            .returning(hot.seq)
        ).all()
        if sorted(removed) != list(range(block.first_seq, block.through_seq + 1)):
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Receipt compaction batch changed", 503)
        state = AgentArchiveState
        changed = self.session.scalar(
            update(state)
            .where(
                and_(
                    state.agent_id == block.agent_id,
                    state.receipts_compacted_through_seq == previous,
                    state.retired_through_seq >= block.through_seq,
                )
            )
            .values(receipts_compacted_through_seq=block.through_seq)
            .returning(state.receipts_compacted_through_seq)
        )
        if changed != block.through_seq:
            raise DomainError("CONFLICT", "Receipt compaction prefix changed", 409)
