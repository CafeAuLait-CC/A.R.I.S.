"""Original acknowledgments are Vega facts; Core does not inspect these tables."""

import hashlib
import json
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.contracts import Contract, DomainError
from aris.modules.vega.models import AgentArchiveState
from aris.modules.vega.receipt_block_repo import ReceiptBlockRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


class RetiredReceipt(Contract):
    seq: int = Field(ge=1)
    message_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    applied_to_current: bool = Field(strict=True)


class RetiredReceiptBatch(Contract):
    source: UsageArchiveHeadQuery
    receipts: tuple[RetiredReceipt, ...] = Field(min_length=1, max_length=1024)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(b"aris.vega.retired-receipts\x00" + raw.encode()).hexdigest()


def retired_receipts_in_transaction(
    db: Session, source: UsageArchiveHeadQuery, *, first: int, last: int
) -> RetiredReceiptBatch:
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    if first < 1 or not 1 <= last - first + 1 <= 1024:
        raise ValueError("Retired acknowledgment lookup requires a bounded positive range")
    boundary = db.scalar(
        select(AgentArchiveState.retired_through_seq)
        .where(
            AgentArchiveState.agent_id == source.agent_id,
            AgentArchiveState.node_id == source.node_id,
        )
        .with_for_update(read=True)
    )
    if boundary is None or last > boundary:
        raise DomainError("CONFLICT", "Original payloads have not left this source range", 409)
    repo = ReceiptBlockRepository(db)
    records = tuple(
        row
        for start in range(first, last + 1, 128)
        for row in repo.original(source.agent_id, start, min(start + 127, last))
    )
    if tuple(row.seq for row in records) != tuple(range(first, last + 1)):
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Retired source acknowledgments are incomplete", 503
        )
    return RetiredReceiptBatch(
        source=source,
        receipts=tuple(
            RetiredReceipt(
                seq=row.seq,
                message_id=row.message_id,
                fingerprint=row.fingerprint,
                applied_to_current=row.applied_to_current,
            )
            for row in records
        ),
    )
