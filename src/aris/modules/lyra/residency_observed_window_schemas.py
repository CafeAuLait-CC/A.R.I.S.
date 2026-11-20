"""Original interval evidence in a fixed open or closed run; never a ledger posting."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_sessions import (
    ResidencyFormationRule,
    ResidencySessionCandidate,
    residency_fingerprint,
)
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_schemas import UsageObservationQuery


class ResidencyObservedWindowReference(IntervalContract):
    reference: ResidencyRunReference
    attestation_id: UUID
    expected_attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_trace_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    after_interval: int = Field(ge=0, le=9223372036854775806, strict=True)
    through_interval: int = Field(ge=1, le=9223372036854775807, strict=True)

    @model_validator(mode="after")
    def bounded_window(self) -> "ResidencyObservedWindowReference":
        if not 1 <= self.through_interval - self.after_interval <= 128:
            raise ValueError("observed residency window requires 1 to 128 original intervals")
        return self


class VerifiedResidencyObservedWindow(IntervalContract):
    assessment_scope: Literal["source_verified_observed_residency_window"] = (
        "source_verified_observed_residency_window"
    )
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    platform_owner_id: None = None
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: ResidencyObservedWindowReference
    consumer_id: str = Field(min_length=1, max_length=128)
    consumer_revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    record_created_at: AwareDatetime
    state_at_record: Literal["open", "closed"]
    parent_run: ResidencyRun = Field(repr=False)
    parent_candidate_state: Literal["stable_candidate", "insufficient_evidence"]
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    candidate: ResidencySessionCandidate = Field(repr=False)
    sources: tuple[UsageObservationQuery, ...] = Field(min_length=2, max_length=1024, repr=False)
    verified_first_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    verified_last_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    source_count: int = Field(ge=2, le=8192, strict=True)
    source_characters: int = Field(ge=1, le=4194304, strict=True)
    manifest_records: int = Field(ge=1, le=512, strict=True)
    index_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("record_created_at", "read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fixed_original_window(self) -> "VerifiedResidencyObservedWindow":
        run, window, query = self.parent_run, self.candidate, self.request
        first, last = window.evidence[0].previous, window.evidence[-1].current

        def enough(intervals: int, micros: int) -> bool:
            return (
                intervals >= self.formation.minimum_intervals
                and micros >= self.formation.minimum_duration_seconds * 1_000_000
            )

        keys = {(item.message_id, item.expected_seq) for item in self.sources}
        if (
            run.id != query.reference.run_id
            or query.through_interval > run.intervals
            or run.rule_fingerprint != residency_fingerprint(self.continuity, self.formation)
            or (self.parent_candidate_state == "stable_candidate")
            != enough(run.intervals, run.observed_microseconds)
            or (window.state == "stable_candidate")
            != enough(len(window.evidence), window.observed_microseconds)
            or len(window.evidence) != query.through_interval - query.after_interval
            or (window.gpu_uuid, window.uid, window.username, window.model)
            != (run.gpu_uuid, run.uid, run.username, run.model)
            or not self.record_created_at <= query.reference.source_cutoff <= self.read_at
            or run.last.compute_observed_at > query.reference.source_cutoff
            or not self.verified_first_seq <= first.seq < last.seq <= self.verified_last_seq
            or self.source_count != self.verified_last_seq - self.verified_first_seq + 1
            or first.compute_observed_at < run.first.compute_observed_at
            or last.compute_observed_at > run.last.compute_observed_at
            or (query.after_interval == 0 and first != run.first)
            or (query.through_interval == run.intervals and last != run.last)
            or self.sources[0].message_id != first.message_id
            or self.sources[-1].message_id != last.message_id
            or len(keys) != len(self.sources)
            or len({item.message_id for item in self.sources}) != len(self.sources)
            or any(
                (item.agent_id, item.node_id) != (run.first.agent_id, run.first.node_id)
                or not first.seq <= item.expected_seq <= last.seq
                for item in self.sources
            )
            or any(
                a.expected_seq >= b.expected_seq
                for a, b in zip(self.sources, self.sources[1:], strict=False)
            )
            or any(
                (item.message_id, item.seq) not in keys
                for edge in window.evidence
                for item in (edge.previous, edge.current)
            )
            or window.end_boundary != "trace_end"
            or window.boundary_reason != "TRACE_END"
            or self.fingerprint != observed_window_fingerprint(self)
        ):
            raise ValueError("observed residency window differs from its fixed original prefix")
        return self


def observed_window_fingerprint(value: VerifiedResidencyObservedWindow) -> str:
    return trace_fingerprint(
        "verified_observed_residency_window",
        canonical_trace(value, exclude={"fingerprint", "read_at"}),
    )
