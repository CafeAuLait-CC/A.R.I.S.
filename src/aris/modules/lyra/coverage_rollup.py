"""Calendar summaries from fixed coverage intervals, without reopening raw samples."""

from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from aris.modules.lyra.daily_session_occupancy import DailySessionOccupancy
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_reader_schemas import SavedWindowView
from aris.modules.lyra.window_schemas import UsageWindowQuery

CoverageGrain = Literal["day", "week"]
CoverageState = Literal["observed_idle", "recorded", "unknown", "uncovered"]


class CoverageDuration(IntervalContract):
    gpu_uuid: str
    state: CoverageState
    owner_id: UUID | None
    microseconds: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def attributed(self) -> "CoverageDuration":
        if (self.state == "recorded") != (self.owner_id is not None):
            raise ValueError("Only recorded duration can have an owner")
        return self


class CoverageBucket(IntervalContract):
    local_period_start: date
    start_at: AwareDatetime
    end_at: AwareDatetime
    durations: tuple[CoverageDuration, ...]


class SavedCoverageRollup(IntervalContract):
    assessment_scope: Literal["saved_coverage_rollup"] = "saved_coverage_rollup"
    grain: CoverageGrain
    query: UsageWindowQuery
    detail_scope: Literal["mine", "node"]
    bucket_coverage: Literal["node_cells", "owned_recorded_cells"]
    snapshot_id: UUID
    snapshot_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    view_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source_prefix_complete: bool
    coverage: Literal["complete", "partial", "unknown"]
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)
    buckets: tuple[CoverageBucket, ...] = Field(min_length=1, max_length=368)
    daily_session: DailySessionOccupancy | None = None
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def optional_session(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.daily_session is None:
            data.pop("daily_session", None)
        return data

    @model_validator(mode="after")
    def session_matches_day(self) -> "SavedCoverageRollup":
        if self.daily_session is not None and (
            self.grain != "day"
            or self.detail_scope != "node"
            or len(self.buckets) != 1
            or (self.daily_session.start_at, self.daily_session.end_at)
            != (self.query.start_at, self.query.end_at)
            or self.daily_session.recorded_microseconds != self.recorded_microseconds
        ):
            raise ValueError("Session evidence differs from the coverage day")
        return self

    def content_fingerprint(self) -> str:
        return trace_fingerprint(
            "saved_coverage_rollup", canonical_trace(self, exclude={"fingerprint"})
        )


def _periods(
    query: UsageWindowQuery, grain: CoverageGrain
) -> list[tuple[date, datetime, datetime]]:
    zone = ZoneInfo(query.timezone)
    first = query.start_at.astimezone(zone).date()
    last = (query.end_at - MICROSECOND).astimezone(zone).date()
    stride = 1 if grain == "day" else 7
    if grain == "week":
        first -= timedelta(days=first.weekday())
    dates = range(0, (last - first).days + 1, stride)
    boundaries = calendar_boundaries(
        first, first + timedelta(days=dates[-1] + stride - 1), query.timezone
    )
    return [
        (first + timedelta(days=index), start, end)
        for index in dates
        if (start := max(query.start_at, boundaries[index]))
        < (end := min(query.end_at, boundaries[index + stride]))
    ]


def roll_up_saved_coverage(view: SavedWindowView, grain: CoverageGrain) -> SavedCoverageRollup:
    """Split coverage cells once at calendar boundaries and sum their exact durations."""
    if grain not in ("day", "week"):
        raise ValueError("Unsupported coverage grain")
    periods = _periods(view.query, grain)
    ends = [period[2] for period in periods]
    totals: list[dict[tuple[str, CoverageState, UUID | None], int]] = [
        defaultdict(int) for _ in periods
    ]
    original_totals: dict[str, int] = defaultdict(int)
    for cell in view.cells:
        if cell.start_at < view.query.start_at or cell.end_at > view.query.end_at:
            raise ValueError("Saved coverage cell is outside the requested range")
        if view.detail_scope == "mine" and (
            cell.state != "recorded" or cell.owner_id != view.query.owner_id
        ):
            raise ValueError("Own coverage can only contain the selected owner's records")
        key = cell.gpu_uuid, cell.state, cell.owner_id
        split_total = 0
        for index in range(bisect_right(ends, cell.start_at), len(periods)):
            _, start, end = periods[index]
            if start >= cell.end_at:
                break
            amount = (min(end, cell.end_at) - max(start, cell.start_at)) // MICROSECOND
            totals[index][key] += amount
            split_total += amount
        if split_total != cell.microseconds:
            raise ValueError("Calendar split did not preserve source coverage")
        original_totals[cell.state] += split_total
    verified = sum(
        cell.microseconds
        for cell in view.cells
        if cell.state == "recorded"
        and (view.query.owner_id is None or cell.owner_id == view.query.owner_id)
    )
    if verified != view.verified_usage_microseconds or (
        view.detail_scope == "node"
        and (
            original_totals["recorded"] + original_totals["observed_idle"],
            original_totals["unknown"],
            original_totals["uncovered"],
        )
        != (
            view.covered_gpu_microseconds,
            view.unknown_gpu_microseconds,
            view.uncovered_gpu_microseconds,
        )
    ):
        raise ValueError("Saved coverage cells and totals differ")
    buckets = tuple(
        CoverageBucket(
            local_period_start=day,
            start_at=start,
            end_at=end,
            durations=tuple(
                CoverageDuration(gpu_uuid=gpu, state=state, owner_id=owner, microseconds=amount)
                for (gpu, state, owner), amount in sorted(
                    values.items(), key=lambda value: (value[0][0], value[0][1], str(value[0][2]))
                )
            ),
        )
        for (day, start, end), values in zip(periods, totals, strict=True)
    )
    result = SavedCoverageRollup(
        grain=grain,
        query=view.query,
        detail_scope=view.detail_scope,
        bucket_coverage="node_cells" if view.detail_scope == "node" else "owned_recorded_cells",
        snapshot_id=view.id,
        snapshot_fingerprint=view.snapshot_fingerprint,
        view_fingerprint=view.view_fingerprint,
        source_prefix_complete=view.source_prefix_complete,
        coverage=view.coverage,
        recorded_microseconds=view.recorded_microseconds,
        verified_usage_microseconds=view.verified_usage_microseconds,
        total_usage_microseconds=view.total_usage_microseconds,
        buckets=buckets,
        fingerprint="0" * 64,
    )
    return result.model_copy(update={"fingerprint": result.content_fingerprint()})
