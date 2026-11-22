"""Keep confirmed session gaps and their clipping at a fixed reporting window."""

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.continuous_usage import ContinuousUsageHistory
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.session_occupancy import SessionOccupancyRule, SessionOccupancySummary
from aris.modules.lyra.usage_intervals import IntervalContract


class RetainedSessionGap(IntervalContract):
    owner_id: UUID
    gpu_uuid: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    before_entry_id: UUID
    after_entry_id: UUID
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def duration(self) -> "RetainedSessionGap":
        if (
            not timedelta(0) < self.end_at - self.start_at <= timedelta(seconds=60)
            or self.before_entry_id == self.after_entry_id
        ):
            raise ValueError("Retained session gap has invalid duration or boundaries")
        return self

    @property
    def microseconds(self) -> int:
        return (self.end_at - self.start_at) // MICROSECOND


class DailySessionOccupancy(IntervalContract):
    start_at: AwareDatetime
    end_at: AwareDatetime
    rule: SessionOccupancyRule
    adjacent_context_loaded: bool
    history_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    recorded_microseconds: int = Field(ge=0)
    unverified_gap_microseconds: int = Field(ge=0)
    retained_gaps: tuple[RetainedSessionGap, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def bounds(self) -> "DailySessionOccupancy":
        if (
            not self.start_at < self.end_at
            or len({(gap.before_entry_id, gap.after_entry_id) for gap in self.retained_gaps})
            != len(self.retained_gaps)
            or any(
                gap.start_at < self.start_at or gap.end_at > self.end_at
                for gap in self.retained_gaps
            )
        ):
            raise ValueError("Daily session gaps must stay within their reporting bounds")
        return self

    @property
    def retained_microseconds(self) -> int:
        return sum(gap.microseconds for gap in self.retained_gaps)


def clip_session_occupancy(
    view: ContinuousUsageHistory,
    evidence: SessionOccupancySummary,
    start_at: datetime,
    end_at: datetime,
) -> DailySessionOccupancy:
    history = view.history
    if (
        evidence.history_fingerprint != history.fingerprint
        or evidence.as_of != history.read_at
        or len(evidence.gaps) != len(view.gaps)
        or not history.start_at <= start_at < end_at <= history.end_at
    ):
        raise ValueError("Session gap evidence does not match its original history")
    padding = timedelta(seconds=evidence.rule.maximum_interruption_seconds)
    retained = []
    unverified = 0
    for gap, assessment in zip(view.gaps, evidence.gaps, strict=True):
        if (
            assessment.interval_fingerprint != gap.interval_fingerprint
            or assessment.before_entry_id != str(gap.before.entry_id)
            or assessment.after_entry_id != str(gap.after.entry_id)
            or assessment.duration_microseconds
            != (gap.after.start_at - gap.before.end_at) // MICROSECOND
        ):
            raise ValueError("Session gap differs from its verified evidence")
        start, end = max(start_at, gap.before.end_at), min(end_at, gap.after.start_at)
        if start >= end:
            continue
        if assessment.state == "retained":
            assert assessment.source_fingerprint is not None
            assert assessment.identity_fingerprint is not None
            retained.append(
                RetainedSessionGap(
                    owner_id=gap.before.owner_id,
                    gpu_uuid=gap.before.gpu_uuid,
                    start_at=start,
                    end_at=end,
                    before_entry_id=gap.before.entry_id,
                    after_entry_id=gap.after.entry_id,
                    source_fingerprint=assessment.source_fingerprint,
                    identity_fingerprint=assessment.identity_fingerprint,
                )
            )
        elif assessment.state == "unknown":
            unverified += (end - start) // MICROSECOND
    return DailySessionOccupancy(
        start_at=start_at,
        end_at=end_at,
        rule=evidence.rule,
        adjacent_context_loaded=(
            history.start_at <= start_at - padding and history.end_at >= end_at + padding
        ),
        history_fingerprint=history.fingerprint,
        evidence_fingerprint=evidence.fingerprint,
        recorded_microseconds=sum(
            max(0, (min(end_at, row.end_at) - max(start_at, row.start_at)) // MICROSECOND)
            for row in history.rows
        ),
        unverified_gap_microseconds=unverified,
        retained_gaps=tuple(retained),
    )
