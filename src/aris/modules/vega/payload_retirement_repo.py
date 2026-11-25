"""Vega owns replacing old payload rows with their original replay acknowledgments."""

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.vega.archive_guard_service import lock_archive_retirement_in_transaction
from aris.modules.vega.models import (
    AgentArchiveState,
    AgentMessage,
    AgentMessageReceipt,
    Node,
    NodeResource,
)
from aris.modules.vega.receipt_block_repo import ReceiptBlockRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


@dataclass(frozen=True)
class RetiredPayload:
    id: str
    seq: int
    message_id: str
    fingerprint: str
    applied_to_current: bool


@dataclass(frozen=True)
class RetirementSourceBoundary:
    protected_seq: int
    retired_through_seq: int


class PayloadRetirementRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_source(self, source: UsageArchiveHeadQuery) -> RetirementSourceBoundary:
        retired = lock_archive_retirement_in_transaction(self.session, source)
        row = self.session.execute(
            select(Node.last_seq, Node.telemetry_seq, Node.heartbeat_seq, Node.collector_health_seq)
            .where(Node.id == source.node_id, Node.agent_id == source.agent_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise DomainError("CONFLICT", "Retirement source binding differs", 409)
        resource = self.session.scalar(
            select(NodeResource.seq).where(NodeResource.node_id == source.node_id)
        )
        protected = min(
            (
                value
                for value in (
                    row.last_seq,
                    row.telemetry_seq,
                    row.heartbeat_seq,
                    row.collector_health_seq,
                    resource,
                )
                if value is not None and value > 0
            ),
            default=0,
        )
        return RetirementSourceBoundary(protected, retired)

    def original_rows(
        self, source: UsageArchiveHeadQuery, first: int, last: int
    ) -> tuple[RetiredPayload, ...]:
        row = AgentMessage
        return tuple(
            RetiredPayload(*value)
            for value in self.session.execute(
                select(
                    row.id,
                    row.seq,
                    row.message_id,
                    row.fingerprint,
                    row.applied_to_current,
                )
                .where(
                    row.agent_id == source.agent_id,
                    row.node_id == source.node_id,
                    row.seq.between(first, last),
                )
                .order_by(row.seq)
            )
        )

    def original_receipts(
        self, agent_id: str, first: int, last: int
    ) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            (row.seq, row.message_id, row.fingerprint)
            for row in ReceiptBlockRepository(self.session).original(agent_id, first, last)
        )

    def replace(self, source: UsageArchiveHeadQuery, rows: tuple[RetiredPayload, ...]) -> None:
        self.session.add_all(
            AgentMessageReceipt(
                agent_id=source.agent_id,
                seq=row.seq,
                message_id=row.message_id,
                fingerprint=row.fingerprint,
                applied_to_current=row.applied_to_current,
            )
            for row in rows
        )
        self.session.flush()
        deleted = self.session.scalars(
            delete(AgentMessage)
            .where(AgentMessage.id.in_(tuple(row.id for row in rows)))
            .returning(AgentMessage.id)
        ).all()
        if set(deleted) != {row.id for row in rows}:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Retirement batch changed; retry", 503)
        state = AgentArchiveState
        through = self.session.scalar(
            update(state)
            .where(
                state.agent_id == source.agent_id,
                state.node_id == source.node_id,
                state.retired_through_seq == rows[0].seq - 1,
            )
            .values(retired_through_seq=rows[-1].seq)
            .returning(state.retired_through_seq)
        )
        if through != rows[-1].seq:
            raise DomainError("CONFLICT", "Archive retired prefix differs from its boundary", 409)
