"""Recorded statistics from service-verified original ledger indexes, never source completeness."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.ledger_window_read import LedgerWindowRead
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.usage_history import (
    UsageHistory,
    UsageHistoryQuery,
    UsageHistoryRow,
    _history_from_rows,
)
from aris.modules.lyra.usage_intervals import IntervalContract


class IndexedUsageSummary(IntervalContract):
    assessment_scope: Literal["recorded_usage_only"] = "recorded_usage_only"
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    node_id: str
    owner_id: UUID | None
    start_at: AwareDatetime
    end_at: AwareDatetime
    snapshot: UsageLedgerHead
    read_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


def _verified(query: UsageHistoryQuery, window: LedgerWindowRead) -> None:
    if (
        (query.node_id, query.start_at, query.end_at)
        != (window.query.node_id, window.query.start_at, window.query.end_at)
        or (query.snapshot is not None and query.snapshot != window.head)
        or window.verification_scope != "verified_ledger_index_and_original_tail"
    ):
        raise ValueError("indexed statistics differ from their service-verified window")


def indexed_usage_summary(
    query: UsageHistoryQuery, window: LedgerWindowRead
) -> IndexedUsageSummary:
    query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
    _verified(query, window)
    return IndexedUsageSummary(
        node_id=query.node_id,
        owner_id=window.query.owner_id,
        start_at=query.start_at,
        end_at=query.end_at,
        snapshot=window.head,
        read_at=window.read_at,
        recorded_microseconds=sum(
            (min(entry.end_at, query.end_at) - max(entry.start_at, query.start_at)) // MICROSECOND
            for entry in window.active
        ),
        gpu_count=len({entry.gpu_uuid for entry in window.active}),
        segment_count=len(window.active),
    )


def indexed_usage_history(query: UsageHistoryQuery, window: LedgerWindowRead) -> UsageHistory:
    query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
    _verified(query, window)
    rows = []
    for entry in window.active:
        if entry.source_derivation_id is None or (
            window.query.owner_id is not None and entry.owner_id != window.query.owner_id
        ):
            raise ValueError("indexed statistics lack their original measurement source")
        start, end = max(query.start_at, entry.start_at), min(query.end_at, entry.end_at)
        if end <= start:
            raise ValueError("indexed statistics contain an unrelated measurement")
        rows.append(
            UsageHistoryRow(
                entry_id=entry.entry_id,
                owner_id=entry.owner_id,
                gpu_uuid=entry.gpu_uuid,
                model=entry.model,
                start_at=start,
                end_at=end,
                recorded_microseconds=(end - start) // MICROSECOND,
                source_derivation_id=entry.source_derivation_id,
                entry_fingerprint=entry.fingerprint,
            )
        )
    return _history_from_rows(
        query,
        owner_id=window.query.owner_id,
        head=window.head,
        rows=tuple(rows),
        read_at=window.read_at,
        method="indexed_usage_history",
    )
