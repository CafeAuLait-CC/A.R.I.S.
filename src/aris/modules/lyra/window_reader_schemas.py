"""Saved result presentation never grants access to other users' detailed intervals."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_schemas import UsageWindowQuery, WindowCoverageCell


class WindowPageCursor(IntervalContract):
    created_at: AwareDatetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class WindowListQuery(UsageWindowQuery):
    before: WindowPageCursor | None = None
    limit: int = Field(default=16, ge=1, le=32, strict=True)


class SavedWindowSummary(IntervalContract):
    id: UUID
    node_id: str
    owner_id: UUID | None
    start_at: AwareDatetime
    end_at: AwareDatetime
    created_at: AwareDatetime
    coverage: Literal["complete", "partial", "unknown"]
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)

    @model_validator(mode="after")
    def honest(self) -> "SavedWindowSummary":
        if (
            not self.start_at < self.end_at <= self.created_at
            or self.verified_usage_microseconds > self.recorded_microseconds
            or self.total_usage_microseconds
            != (self.verified_usage_microseconds if self.coverage == "complete" else None)
        ):
            raise ValueError("saved window summary is inconsistent")
        return self


class SavedWindowPage(IntervalContract):
    query: WindowListQuery
    items: tuple[SavedWindowSummary, ...]
    next_cursor: WindowPageCursor | None = None


class SavedWindowView(IntervalContract):
    assessment_scope: Literal["saved_usage_window_view"] = "saved_usage_window_view"
    id: UUID
    query: UsageWindowQuery
    created_at: AwareDatetime
    captured_at: AwareDatetime
    detail_scope: Literal["mine", "node"]
    coverage: Literal["complete", "partial", "unknown"]
    source_prefix_complete: bool
    recorded_microseconds: int
    verified_usage_microseconds: int
    total_usage_microseconds: int | None
    covered_gpu_microseconds: int
    unknown_gpu_microseconds: int
    uncovered_gpu_microseconds: int
    cells: tuple[WindowCoverageCell, ...]
    source_fingerprint: str
    snapshot_fingerprint: str
