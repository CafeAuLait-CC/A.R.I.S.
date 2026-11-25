"""Finite trusted maintenance; the Collector's replay protocol stays unchanged."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.modules.vega.models import AgentArchiveState, AgentMessage, AgentReceiptBlock
from aris.modules.vega.payload_retirement_repo import PayloadRetirementRepository
from aris.modules.vega.payload_retirement_schemas import PayloadRetirementContext
from aris.modules.vega.receipt_block_codec import block_fingerprint, encode_receipts
from aris.modules.vega.receipt_block_repo import ReceiptBlockRepository, decoded
from aris.modules.vega.receipt_compaction_schemas import (
    ReceiptCompactionRequest,
    ReceiptCompactionResult,
)
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery

ACTION = "vega.archive.receipts_compacted"


class VegaReceiptMaintenance:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def prepare(
        self, source: UsageArchiveHeadQuery, operation_id: UUID
    ) -> ReceiptCompactionRequest | None:
        source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
        with self.sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            transaction_budget(db, 1000, 3000, 10000)
            state = db.get(AgentArchiveState, source.agent_id)
            if state is None or state.node_id != source.node_id:
                raise DomainError("CONFLICT", "Receipt compaction source binding differs", 409)
            first = state.receipts_compacted_through_seq + 1
            last = min(state.retired_through_seq, first + 127)
            if last < first:
                return None
            rows = ReceiptBlockRepository(db).hot(source.agent_id, first, last)
            if tuple(row.seq for row in rows) != tuple(range(first, last + 1)):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Original receipt prefix is incomplete", 503
                )
            packed = encode_receipts(rows)
            return ReceiptCompactionRequest(
                id=operation_id,
                source=source,
                first_seq=first,
                through_seq=last,
                block_fingerprint=block_fingerprint(source.agent_id, first, last, packed),
            )

    def compact(
        self, context: PayloadRetirementContext, request: ReceiptCompactionRequest
    ) -> ReceiptCompactionResult:
        try:
            with self.sessions() as db:
                transaction_budget(db, 1000, 3000, 15000)
                result = self.compact_in_transaction(db, context, request)
                commit_owned_transaction(db)
                return result
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Receipt compaction is unavailable", 503
            ) from None

    def compact_in_transaction(
        self, db: Session, context: PayloadRetirementContext, request: ReceiptCompactionRequest
    ) -> ReceiptCompactionResult:
        context = PayloadRetirementContext.model_validate(context.model_dump(mode="json"))
        request = ReceiptCompactionRequest.model_validate(request.model_dump(mode="json"))
        source = request.source
        PayloadRetirementRepository(db).lock_source(source)
        repo = ReceiptBlockRepository(db)
        existing = db.get(AuditEntry, str(request.id))
        stored = db.get(AgentReceiptBlock, str(request.id))
        if existing is not None or stored is not None:
            if (
                existing is None
                or stored is None
                or (
                    existing.actor != context.actor
                    or existing.action != ACTION
                    or existing.target != source.node_id
                    or existing.changes != request.model_dump(mode="json")
                    or repo.compacted_through(source.agent_id) < request.through_seq
                    or repo.hot(source.agent_id, request.first_seq, request.through_seq)
                    or (stored.agent_id, stored.first_seq, stored.through_seq, stored.fingerprint)
                    != (
                        source.agent_id,
                        request.first_seq,
                        request.through_seq,
                        request.block_fingerprint,
                    )
                )
            ):
                raise DomainError(
                    "CONFLICT", "Receipt compaction differs from its saved batch", 409
                )
            decoded(stored)
            return ReceiptCompactionResult(
                request=request,
                compacted_receipts=request.through_seq - request.first_seq + 1,
                duplicate=True,
            )
        previous = repo.compacted_through(source.agent_id)
        retired = db.scalar(
            select(AgentArchiveState.retired_through_seq).where(
                AgentArchiveState.agent_id == source.agent_id
            )
        )
        if previous != request.first_seq - 1 or retired is None or request.through_seq > retired:
            raise DomainError(
                "CONFLICT", "Receipt compaction is not the oldest retired prefix", 409
            )
        raw = db.scalar(
            select(AgentMessage.seq)
            .where(
                AgentMessage.agent_id == source.agent_id,
                AgentMessage.seq.between(request.first_seq, request.through_seq),
            )
            .limit(1)
        )
        rows = repo.hot(source.agent_id, request.first_seq, request.through_seq)
        if raw is not None or tuple(row.seq for row in rows) != tuple(
            range(request.first_seq, request.through_seq + 1)
        ):
            raise DomainError(
                "CONFLICT", "Receipt compaction requires the complete retired prefix", 409
            )
        packed = encode_receipts(rows)
        if (
            block_fingerprint(source.agent_id, request.first_seq, request.through_seq, packed)
            != request.block_fingerprint
        ):
            raise DomainError(
                "CONFLICT", "Receipt compaction original acknowledgments changed", 409
            )
        repo.replace(
            AgentReceiptBlock(
                id=str(request.id),
                agent_id=source.agent_id,
                first_seq=request.first_seq,
                through_seq=request.through_seq,
                packed_receipts=packed,
                message_ids=[row.message_id for row in rows],
                fingerprint=request.block_fingerprint,
            ),
            previous,
        )
        db.add(
            AuditEntry(
                id=str(request.id),
                actor=context.actor,
                action=ACTION,
                target=source.node_id,
                request_id=context.request_id,
                recorded_at=datetime.now(UTC),
                changes=request.model_dump(mode="json"),
            )
        )
        db.flush()
        return ReceiptCompactionResult(
            request=request, compacted_receipts=len(rows), duplicate=False
        )
