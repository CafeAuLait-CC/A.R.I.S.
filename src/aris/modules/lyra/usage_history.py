"""User and calendar summaries of the existing immutable recorded-use ledger."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator

from aris.core.access.schemas import UserPrincipal
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    UsageLedgerEntry,
    UsageLedgerHead,
    canonical_ledger,
    ledger_fingerprint,
)
from aris.modules.lyra.ledger_window_read import MAX_LEDGER_WINDOW_ENTRIES
from aris.modules.lyra.recorded_usage import (
    MICROSECOND,
    RecordedUsageQuery,
    _active_prefix,
)
from aris.modules.lyra.usage_intervals import IntervalContract


class UsageHistoryQuery(RecordedUsageQuery):
    timezone: str = Field(default="UTC", min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        return UserPrincipal.iana_timezone(value)


class UsageHistoryRow(IntervalContract):
    entry_id: UUID
    owner_id: UUID
    gpu_uuid: str
    model: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    recorded_microseconds: int = Field(gt=0)
    source_derivation_id: UUID
    entry_fingerprint: str


class UsageHistoryOwner(IntervalContract):
    owner_id: UUID
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class UsageHistoryDay(IntervalContract):
    date: date
    recorded_microseconds: int = Field(ge=0)


class UsageHistoryGpu(IntervalContract):
    gpu_uuid: str
    models: tuple[str, ...]
    recorded_microseconds: int = Field(gt=0)
    owner_count: int = Field(ge=1)
    segment_count: int = Field(ge=1)


def usage_gpu_summary(rows: tuple[UsageHistoryRow, ...]) -> tuple[UsageHistoryGpu, ...]:
    """Presentation of already-authorized history; no new metering source."""
    by_gpu: dict[str, list[UsageHistoryRow]] = defaultdict(list)
    for row in rows:
        by_gpu[row.gpu_uuid].append(row)
    return tuple(
        sorted(
            (
                UsageHistoryGpu(
                    gpu_uuid=gpu,
                    models=tuple(sorted({value.model for value in values})),
                    recorded_microseconds=sum(value.recorded_microseconds for value in values),
                    owner_count=len({value.owner_id for value in values}),
                    segment_count=len(values),
                )
                for gpu, values in by_gpu.items()
            ),
            key=lambda item: (-item.recorded_microseconds, item.gpu_uuid),
        )
    )


class UsageHistory(IntervalContract):
    assessment_scope: Literal["recorded_usage_only"] = "recorded_usage_only"
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    node_id: str
    owner_id: UUID | None
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: str
    snapshot: UsageLedgerHead
    read_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    owners: tuple[UsageHistoryOwner, ...] = Field(max_length=128)
    days: tuple[UsageHistoryDay, ...] = Field(max_length=368)
    rows: tuple[UsageHistoryRow, ...] = Field(max_length=MAX_LEDGER_WINDOW_ENTRIES)
    fingerprint: str


def calendar_boundaries(start: date, end: date, timezone: str) -> tuple[datetime, ...]:
    """UTC instants for local midnights, including DST and skipped local dates."""
    zone = ZoneInfo(timezone)
    return tuple(
        datetime.combine(start + timedelta(days=index), time.min, zone).astimezone(UTC)
        for index in range((end - start).days + 2)
    )


def usage_history(
    query: UsageHistoryQuery,
    *,
    owner_id: UUID | None,
    head: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    read_at: datetime,
) -> UsageHistory:
    query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
    if read_at.tzinfo is None or read_at.utcoffset() is None:
        raise ValueError("history requires an aware read time")
    read_at = read_at.astimezone(UTC)
    if (
        head.node_id != query.node_id
        or (query.snapshot is not None and query.snapshot != head)
        or read_at < query.end_at
    ):
        raise ValueError("history differs from its requested window or snapshot")
    rows = []
    for entry in _active_prefix(head, entries, read_at):
        assert isinstance(entry.details, PostedMeasurement)
        value = entry.details.measurement
        start, end = max(query.start_at, value.start_at), min(query.end_at, value.end_at)
        if end <= start or (owner_id is not None and value.owner_id != owner_id):
            continue
        rows.append(
            UsageHistoryRow(
                entry_id=entry.id,
                owner_id=value.owner_id,
                gpu_uuid=value.gpu_uuid,
                model=value.model,
                start_at=start,
                end_at=end,
                recorded_microseconds=(end - start) // MICROSECOND,
                source_derivation_id=value.source_derivation_id,
                entry_fingerprint=entry.fingerprint,
            )
        )
    return _history_from_rows(
        query,
        owner_id=owner_id,
        head=head,
        rows=tuple(rows),
        read_at=read_at,
        method="usage_history",
    )


def _history_from_rows(
    query: UsageHistoryQuery,
    *,
    owner_id: UUID | None,
    head: UsageLedgerHead,
    rows: tuple[UsageHistoryRow, ...],
    read_at: datetime,
    method: Literal["usage_history", "indexed_usage_history"],
) -> UsageHistory:
    """Reduce already-verified rows; each public caller must establish its own evidence method."""
    ordered = tuple(sorted(rows, key=lambda row: (row.start_at, row.gpu_uuid, str(row.entry_id))))
    by_owner: dict[UUID, list[UsageHistoryRow]] = defaultdict(list)
    for row in ordered:
        by_owner[row.owner_id].append(row)
    owners = tuple(
        sorted(
            (
                UsageHistoryOwner(
                    owner_id=owner,
                    recorded_microseconds=sum(row.recorded_microseconds for row in values),
                    gpu_count=len({row.gpu_uuid for row in values}),
                    segment_count=len(values),
                )
                for owner, values in by_owner.items()
            ),
            key=lambda value: (-value.recorded_microseconds, str(value.owner_id)),
        )
    )
    zone = ZoneInfo(query.timezone)
    first = query.start_at.astimezone(zone).date()
    last = (query.end_at - MICROSECOND).astimezone(zone).date()
    boundaries = calendar_boundaries(first, last, query.timezone)
    days = tuple(
        UsageHistoryDay(
            date=first + timedelta(days=index),
            recorded_microseconds=sum(
                max(timedelta(0), min(row.end_at, end) - max(row.start_at, start)) // MICROSECOND
                for row in ordered
            ),
        )
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:], strict=False))
    )
    values = dict(
        node_id=query.node_id,
        owner_id=owner_id,
        start_at=query.start_at,
        end_at=query.end_at,
        timezone=query.timezone,
        snapshot=head,
        read_at=read_at,
        recorded_microseconds=sum(row.recorded_microseconds for row in ordered),
        gpu_count=len({row.gpu_uuid for row in ordered}),
        segment_count=len(ordered),
        owners=owners,
        days=days,
        rows=ordered,
        fingerprint="0" * 64,
    )
    result = UsageHistory(**values)
    return result.model_copy(
        update={
            "fingerprint": ledger_fingerprint(
                method, canonical_ledger(result, exclude={"fingerprint", "read_at"})
            )
        }
    )
