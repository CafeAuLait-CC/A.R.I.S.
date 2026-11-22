from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.reporting_gaps import ReportingGap, ReportingPoint
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import MAX_ARCHIVE_SEQ, UsageArchiveHeadQuery

MAX_SAVED_GAPS = 128
MAX_GAP_RECORD_CHARACTERS = 4096


class StoredReportingGap(IntervalContract):
    source: UsageArchiveHeadQuery
    gap: ReportingGap
    threshold_seconds: int = Field(ge=1, le=3600, strict=True)
    recorded_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    def content_fingerprint(self) -> str:
        return trace_fingerprint(
            "reporting_gap_history", canonical_trace(self, exclude={"fingerprint"})
        )

    @model_validator(mode="after")
    def verified_pair(self) -> "StoredReportingGap":
        previous, following = self.gap.previous_report, self.gap.next_report
        observed = following.observed_at - previous.observed_at
        received = following.received_at - previous.received_at
        threshold = timedelta(seconds=self.threshold_seconds)
        if (
            int(following.seq) != int(previous.seq) + 1
            or int(following.seq) > MAX_ARCHIVE_SEQ
            or max(observed, received) < threshold
            or self.gap.large_observed_interval != (observed >= threshold)
            or self.gap.large_received_interval != (received >= threshold)
            or self.fingerprint != self.content_fingerprint()
        ):
            raise ValueError("Stored reporting gap differs from its consecutive archive evidence")
        return self


class ReportingGapCapture(IntervalContract):
    source: UsageArchiveHeadQuery
    source_range_complete: bool
    saved: int = Field(ge=0, le=MAX_SAVED_GAPS)
    already_saved: int = Field(ge=0, le=MAX_SAVED_GAPS)


class ReportingGapScanPosition(IntervalContract):
    source: UsageArchiveHeadQuery
    first_seq: int = Field(ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    last_report: ReportingPoint
    uncertain_clock_pairs: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    def content_fingerprint(self) -> str:
        return trace_fingerprint(
            "reporting_gap_scan", canonical_trace(self, exclude={"fingerprint"})
        )

    @model_validator(mode="after")
    def verified_position(self) -> "ReportingGapScanPosition":
        if (
            not self.first_seq <= int(self.last_report.seq) <= MAX_ARCHIVE_SEQ
            or self.uncertain_clock_pairs > int(self.last_report.seq) - self.first_seq
            or self.fingerprint != self.content_fingerprint()
        ):
            raise ValueError("Reporting scan position is inconsistent")
        return self


class ReportingGapScan(IntervalContract):
    source: UsageArchiveHeadQuery
    first_seq: int
    through_seq: int | None
    archive_head: int
    state: Literal["caught_up", "more", "missing"]
    missing_seq: int | None
    saved: int
    already_saved: int
    uncertain_clock_pairs: int
