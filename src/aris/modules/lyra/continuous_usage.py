"""A compact presentation of verified recorded intervals, never a replacement ledger."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.ledger_schemas import canonical_ledger, ledger_fingerprint
from aris.modules.lyra.ledger_window_read import MAX_LEDGER_WINDOW_ENTRIES
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryRow
from aris.modules.lyra.usage_intervals import IntervalContract

SHORT_INTERRUPTION_MICROSECONDS = 60 * 1000000


@dataclass(frozen=True)
class UsageRowContext:
    entry_id: UUID
    entry_fingerprint: str
    timing_fingerprint: str


@dataclass(frozen=True)
class UsagePresentationGap:
    before: UsageHistoryRow
    after: UsageHistoryRow
    interval_fingerprint: str


class ContinuousUsageInterval(IntervalContract):
    owner_id: UUID
    gpu_uuid: str
    model: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    recorded_microseconds: int = Field(gt=0)
    gap_microseconds: int = Field(default=0, ge=0)
    interruption_count: int = Field(default=0, ge=0)
    source_segment_count: int = Field(ge=1, le=MAX_LEDGER_WINDOW_ENTRIES)
    first_entry_id: UUID
    last_entry_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def continuous_duration(self) -> "ContinuousUsageInterval":
        if (
            (self.end_at - self.start_at) // MICROSECOND
            != self.recorded_microseconds + self.gap_microseconds
            or (self.gap_microseconds == 0) != (self.interruption_count == 0)
            or self.interruption_count >= self.source_segment_count
            or self.gap_microseconds > self.interruption_count * SHORT_INTERRUPTION_MICROSECONDS
        ):
            raise ValueError("grouped recorded usage must keep gaps outside recorded duration")
        return self


class _IntervalMembers(IntervalContract):
    node_id: str
    timing_fingerprint: str
    maximum_interruption_microseconds: int = SHORT_INTERRUPTION_MICROSECONDS
    rows: tuple[UsageHistoryRow, ...] = Field(min_length=1, max_length=MAX_LEDGER_WINDOW_ENTRIES)


@dataclass(frozen=True)
class ContinuousUsageHistory:
    history: UsageHistory
    intervals: tuple[ContinuousUsageInterval, ...]
    gaps: tuple[UsagePresentationGap, ...] = ()


def continuous_usage_history(
    history: UsageHistory, contexts: tuple[UsageRowContext, ...]
) -> ContinuousUsageHistory:
    by_id = {value.entry_id: value for value in contexts}
    if len(by_id) != len(contexts):
        raise ValueError("recorded usage contexts contain duplicate originals")
    groups: list[tuple[str, list[UsageHistoryRow]]] = []
    previous: dict[str, int] = {}
    for row in history.rows:
        context = by_id.get(row.entry_id)
        if context is None or context.entry_fingerprint != row.entry_fingerprint:
            raise ValueError("continuous usage lacks the original row's timing context")
        index = previous.get(row.gpu_uuid)
        prior = groups[index][1][-1] if index is not None else None
        joins = (
            prior is not None
            and index is not None
            and (prior.owner_id, prior.model, groups[index][0])
            == (row.owner_id, row.model, context.timing_fingerprint)
            and 0 <= (row.start_at - prior.end_at) // MICROSECOND <= SHORT_INTERRUPTION_MICROSECONDS
        )
        if joins and index is not None:
            groups[index][1].append(row)
        else:
            previous[row.gpu_uuid] = len(groups)
            groups.append((context.timing_fingerprint, [row]))
    intervals = []
    gaps: list[UsagePresentationGap] = []
    for timing, rows in groups:
        first, last = rows[0], rows[-1]
        fingerprint = ledger_fingerprint(
            "continuous_usage_presentation",
            canonical_ledger(
                _IntervalMembers(
                    node_id=history.node_id, timing_fingerprint=timing, rows=tuple(rows)
                )
            ),
        )
        gaps.extend(
            UsagePresentationGap(a, b, fingerprint)
            for a, b in zip(rows, rows[1:], strict=False)
            if b.start_at > a.end_at
        )
        intervals.append(
            ContinuousUsageInterval(
                owner_id=first.owner_id,
                gpu_uuid=first.gpu_uuid,
                model=first.model,
                start_at=first.start_at,
                end_at=last.end_at,
                recorded_microseconds=sum(row.recorded_microseconds for row in rows),
                gap_microseconds=sum(
                    (b.start_at - a.end_at) // MICROSECOND
                    for a, b in zip(rows, rows[1:], strict=False)
                ),
                interruption_count=sum(
                    b.start_at > a.end_at for a, b in zip(rows, rows[1:], strict=False)
                ),
                source_segment_count=len(rows),
                first_entry_id=first.entry_id,
                last_entry_id=last.entry_id,
                fingerprint=fingerprint,
            )
        )
    return ContinuousUsageHistory(
        history,
        tuple(
            sorted(intervals, key=lambda row: (row.start_at, row.gpu_uuid, str(row.first_entry_id)))
        ),
        tuple(gaps),
    )
