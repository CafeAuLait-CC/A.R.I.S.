"""Trusted window data from verified indexes plus a bounded original tail; not an ACL."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from aris.modules.lyra.ledger_schemas import UsageLedgerHead, canonical_ledger, ledger_fingerprint
from aris.modules.lyra.ledger_segment_schemas import LedgerEntryIndex, LedgerSegmentReference
from aris.modules.lyra.recorded_usage import RecordedUsageQuery

MAX_LEDGER_WINDOW_ENTRIES = 32768
MAX_LEDGER_WINDOW_INDEX_BYTES = 32 * 1024 * 1024
MAX_LEDGER_WINDOW_SEGMENTS = 512
MAX_LEDGER_WINDOW_SEGMENT_BYTES = 64 * 1024 * 1024


class LedgerWindowQuery(RecordedUsageQuery):
    owner_id: UUID | None = None


@dataclass(frozen=True)
class LedgerWindowRead:
    """Service-produced comparison input; external callers cannot submit this as authority."""

    query: LedgerWindowQuery
    head: UsageLedgerHead
    read_at: datetime
    verified_at: datetime
    anchor: LedgerSegmentReference | None
    segments: tuple[LedgerSegmentReference, ...]
    entries: tuple[LedgerEntryIndex, ...]
    active: tuple[LedgerEntryIndex, ...]
    reversals: tuple[LedgerEntryIndex, ...]
    fingerprint: str
    verification_scope: Literal["verified_ledger_index_and_original_tail"] = (
        "verified_ledger_index_and_original_tail"
    )
    quota_usage_available: Literal[False] = False


def window_read_fingerprint(query: LedgerWindowQuery, entries: tuple[LedgerEntryIndex, ...]) -> str:
    # Index batching and generation times do not alter the original committed data selection.
    from aris.modules.lyra.usage_intervals import IntervalContract

    class Content(IntervalContract):
        query: LedgerWindowQuery
        members: tuple[tuple[UUID, int, str], ...]

    return ledger_fingerprint(
        "indexed_original_ledger_window",
        canonical_ledger(
            Content(
                query=query,
                members=tuple(
                    (entry.entry_id, entry.revision, entry.fingerprint) for entry in entries
                ),
            )
        ),
    )
