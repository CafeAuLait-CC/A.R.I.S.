"""Describe long intervals between consecutive archived node reports."""

from datetime import timedelta
from itertools import chain, pairwise
from typing import Literal

from pydantic import AwareDatetime, Field

from aris.modules.lyra.activity_preview import ActivityPreview
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference, UsageArchivePage

DEFAULT_REPORTING_GAP_SECONDS = 180


class ReportingPoint(IntervalContract):
    seq: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    kind: Literal["heartbeat", "telemetry"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime
    received_at: AwareDatetime

    @classmethod
    def from_archive(cls, source: ArchivedUsageReference) -> "ReportingPoint":
        return cls(
            seq=str(source.seq),
            kind=source.kind,
            fingerprint=source.fingerprint,
            observed_at=source.observed_at,
            received_at=source.received_at,
        )


class ReportingGap(IntervalContract):
    previous_report: ReportingPoint
    next_report: ReportingPoint
    large_observed_interval: bool
    large_received_interval: bool
    cause: Literal["unknown"] = "unknown"


class ReportingGapPreview(IntervalContract):
    source_preview_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    interval_threshold_seconds: int = Field(ge=1, le=3600, strict=True)
    source_range_complete: bool
    pairs_with_uncertain_clock_order: int = Field(ge=0, le=1023)
    gaps: tuple[ReportingGap, ...] = Field(max_length=1023)


def archived_reporting_gaps(
    preview: ActivityPreview,
    pages: tuple[UsageArchivePage, ...],
    threshold_seconds: int = DEFAULT_REPORTING_GAP_SECONDS,
) -> ReportingGapPreview:
    threshold = timedelta(seconds=threshold_seconds)
    reports = chain.from_iterable((item.reference for item in page.items) for page in pages)
    gaps = []
    uncertain = 0
    for previous, following in pairwise(reports):
        observed = following.observed_at - previous.observed_at
        received = following.received_at - previous.received_at
        if following.seq != previous.seq + 1 or min(observed, received) < timedelta(0):
            uncertain += 1
            continue
        if observed >= threshold or received >= threshold:
            gaps.append(
                ReportingGap(
                    previous_report=ReportingPoint.from_archive(previous),
                    next_report=ReportingPoint.from_archive(following),
                    large_observed_interval=observed >= threshold,
                    large_received_interval=received >= threshold,
                )
            )
    return ReportingGapPreview(
        source_preview_fingerprint=preview.fingerprint,
        interval_threshold_seconds=threshold_seconds,
        source_range_complete=preview.range_complete,
        pairs_with_uncertain_clock_order=uncertain,
        gaps=tuple(gaps),
    )
