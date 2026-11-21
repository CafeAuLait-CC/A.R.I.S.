"""A compact presentation of verified recorded intervals, never a replacement ledger."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.ledger_schemas import canonical_ledger, ledger_fingerprint
from aris.modules.lyra.ledger_window_read import MAX_LEDGER_WINDOW_ENTRIES
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryRow
from aris.modules.lyra.usage_intervals import IntervalContract


@dataclass(frozen=True)
class UsageRowContext:
    entry_id: UUID
    entry_fingerprint: str
    timing_fingerprint: str


class ContinuousUsageInterval(IntervalContract):
    owner_id: UUID
    gpu_uuid: str
    model: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    recorded_microseconds: int = Field(gt=0)
    source_segment_count: int = Field(ge=1, le=MAX_LEDGER_WINDOW_ENTRIES)
    first_entry_id: UUID
    last_entry_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def continuous_duration(self) -> "ContinuousUsageInterval":
        if (self.end_at - self.start_at) // MICROSECOND != self.recorded_microseconds:
            raise ValueError("continuous recorded usage must not fill time gaps")
        return self


class _IntervalMembers(IntervalContract):
    node_id: str
    timing_fingerprint: str
    rows: tuple[UsageHistoryRow, ...] = Field(min_length=1, max_length=MAX_LEDGER_WINDOW_ENTRIES)


@dataclass(frozen=True)
class ContinuousUsageHistory:
    history: UsageHistory
    intervals: tuple[ContinuousUsageInterval, ...]


def continuous_usage_history(
    history: UsageHistory, contexts: tuple[UsageRowContext, ...]
) -> ContinuousUsageHistory:
    by_id = {value.entry_id: value for value in contexts}
    if len(by_id) != len(contexts):
        raise ValueError("recorded usage contexts contain duplicate originals")
    groups: list[tuple[str, list[UsageHistoryRow]]] = []
    previous: dict[tuple[UUID, str, str, str], int] = {}
    for row in history.rows:
        context = by_id.get(row.entry_id)
        if context is None or context.entry_fingerprint != row.entry_fingerprint:
            raise ValueError("continuous usage lacks the original row's timing context")
        key = (row.owner_id, row.gpu_uuid, row.model, context.timing_fingerprint)
        index = previous.get(key)
        if index is not None and groups[index][1][-1].end_at == row.start_at:
            groups[index][1].append(row)
        else:
            previous[key] = len(groups)
            groups.append((context.timing_fingerprint, [row]))
    intervals = []
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
        intervals.append(
            ContinuousUsageInterval(
                owner_id=first.owner_id,
                gpu_uuid=first.gpu_uuid,
                model=first.model,
                start_at=first.start_at,
                end_at=last.end_at,
                recorded_microseconds=sum(row.recorded_microseconds for row in rows),
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
    )
