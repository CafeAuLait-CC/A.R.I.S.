"""Original acknowledgments are Vega facts; Core does not inspect these tables."""

import hashlib
import json
from uuid import UUID

from pydantic import Field
from sqlalchemy.orm import Session

from aris.core.contracts import Contract, DomainError
from aris.modules.vega.receipt_block_repo import ReceiptBlockRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


class OriginalAcknowledgment(Contract):
    seq: int = Field(ge=1)
    message_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    applied_to_current: bool = Field(strict=True)


class OriginalAcknowledgmentBatch(Contract):
    source: UsageArchiveHeadQuery
    receipts: tuple[OriginalAcknowledgment, ...] = Field(min_length=1, max_length=1024)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        # Existing saved maintenance proofs bind logical acknowledgments. Their
        # digest stays stable when the same facts move between storage tiers.
        return hashlib.sha256(b"aris.vega.retired-receipts\x00" + raw.encode()).hexdigest()


def original_receipts_in_transaction(
    db: Session, source: UsageArchiveHeadQuery, *, first: int, last: int
) -> OriginalAcknowledgmentBatch:
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    if first < 1 or not 1 <= last - first + 1 <= 1024:
        raise ValueError("Original acknowledgment lookup requires a bounded positive range")
    repo = ReceiptBlockRepository(db)
    boundary = repo.source_retirement_boundary(source.agent_id, source.node_id)
    if boundary is None:
        raise DomainError("CONFLICT", "Original acknowledgment source does not match", 409)
    records = list(repo.raw_originals(source.agent_id, source.node_id, first, last))
    retired_last = min(last, boundary)
    records.extend(
        row
        for start in range(first, retired_last + 1, 128)
        for row in repo.original(source.agent_id, start, min(start + 127, retired_last))
    )
    records.sort(key=lambda row: row.seq)
    if tuple(row.seq for row in records) != tuple(range(first, last + 1)):
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Original source acknowledgments are incomplete", 503
        )
    return OriginalAcknowledgmentBatch(
        source=source,
        receipts=tuple(
            OriginalAcknowledgment(
                seq=row.seq,
                message_id=row.message_id,
                fingerprint=row.fingerprint,
                applied_to_current=row.applied_to_current,
            )
            for row in records
        ),
    )
