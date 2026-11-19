"""Saved sampled segments and bounded lookup are not complete usage coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.residency_segments import (
    ResidencySegment,
    ResidencySegmentAnalysis,
    ResidencySegmentQuery,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_SEGMENT_SET_BYTES = 8388608
MAX_SEGMENT_LOOKUP_CHARACTERS = 2097152
MAX_SEGMENT_LOOKUP_ITEMS = 128
MAX_SEGMENT_INTERVAL = 9223372036854775807


class RecordResidencySegments(IntervalContract):
    id: UUID
    source: ResidencySegmentQuery


class StoredResidencySegmentSet(IntervalContract):
    assessment_scope: Literal["stored_sampled_residency_segments"] = (
        "stored_sampled_residency_segments"
    )
    quota_usage_available: Literal[False] = False
    request: RecordResidencySegments
    event_id: UUID
    analysis: ResidencySegmentAnalysis = Field(repr=False)
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fixed_set(self) -> "StoredResidencySegmentSet":
        if (
            self.request.source != self.analysis.query
            or self.analysis.read_at > self.created_at
            or self.fingerprint != segment_set_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_SEGMENT_SET_BYTES
        ):
            raise ValueError("stored segments differ from their fixed source analysis")
        return self


class SegmentSetReceipt(IntervalContract):
    value: StoredResidencySegmentSet
    duplicate: bool = Field(strict=True)


class ResidencySegmentLookup(IntervalContract):
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    after_interval: int = Field(ge=0, lt=MAX_SEGMENT_INTERVAL, strict=True)
    through_interval: int = Field(ge=1, le=MAX_SEGMENT_INTERVAL, strict=True)
    limit: int = Field(default=128, ge=1, le=MAX_SEGMENT_LOOKUP_ITEMS, strict=True)

    @model_validator(mode="after")
    def increasing(self) -> "ResidencySegmentLookup":
        if self.through_interval <= self.after_interval:
            raise ValueError("segment lookup requires an increasing interval range")
        return self


class StoredResidencySegmentItem(IntervalContract):
    set_id: UUID
    set_created_at: AwareDatetime
    set_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    segment_index: int = Field(ge=0, lt=16384, strict=True)
    consumer_id: str = Field(min_length=1, max_length=128)
    segment: ResidencySegment = Field(repr=False)


class ResidencySegmentPage(IntervalContract):
    assessment_scope: Literal["stored_candidate_segment_lookup"] = "stored_candidate_segment_lookup"
    quota_usage_available: Literal[False] = False
    original_source_revalidation: Literal["not_performed"] = "not_performed"
    query: ResidencySegmentLookup
    items: tuple[StoredResidencySegmentItem, ...] = Field(
        max_length=MAX_SEGMENT_LOOKUP_ITEMS, repr=False
    )
    next_after_interval: int = Field(ge=0, le=MAX_SEGMENT_INTERVAL, strict=True)
    state: Literal["range_end", "page_end", "gap"]
    missing_interval: int | None = Field(default=None, ge=0, le=MAX_SEGMENT_INTERVAL, strict=True)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def adjoining_unclipped_segments(self) -> "ResidencySegmentPage":
        expected = self.query.after_interval
        previous: ResidencySegment | None = None
        if len(self.items) > self.query.limit:
            raise ValueError("segment page exceeds its item budget")
        for index, item in enumerate(self.items):
            segment = item.segment
            if (
                item.consumer_id != self.query.consumer_id
                or item.set_created_at > self.read_at
                or segment.run_at_segment_end.id != self.query.run_id
                or segment.interval_offset > expected
                or segment.run_at_segment_end.intervals <= expected
                or (index > 0 and segment.interval_offset != expected)
                or (previous is not None and previous.last != segment.first)
                or expected >= self.query.through_interval
            ):
                raise ValueError(
                    "stored segment page has an inconsistent or missing source interval"
                )
            expected = min(segment.run_at_segment_end.intervals, self.query.through_interval)
            previous = segment
        state = (
            "range_end"
            if expected == self.query.through_interval
            else ("page_end" if len(self.items) == self.query.limit else "gap")
        )
        if (
            self.next_after_interval != expected
            or self.state != state
            or self.missing_interval != (expected if state == "gap" else None)
            or self.fingerprint != segment_page_fingerprint(self)
        ):
            raise ValueError("segment page differs from its exact indexed interval progress")
        return self


def segment_set_fingerprint(value: StoredResidencySegmentSet) -> str:
    return trace_fingerprint(
        "stored_residency_segments", canonical_trace(value, exclude={"fingerprint"})
    )


def segment_page_fingerprint(value: ResidencySegmentPage) -> str:
    return trace_fingerprint(
        "residency_segment_page", canonical_trace(value, exclude={"fingerprint", "read_at"})
    )


def segment_index_identity(set_id: UUID, segment: ResidencySegment) -> str:
    return trace_fingerprint("residency_segment_index", str(set_id) + "\n" + segment.id)
