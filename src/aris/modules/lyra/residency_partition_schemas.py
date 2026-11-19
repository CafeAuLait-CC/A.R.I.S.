"""Deterministic bounded pieces retain their closed parent's original authority."""

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


class ResidencyPartitionMethod(IntervalContract):
    method: Literal["balanced_observed_intervals"] = "balanced_observed_intervals"
    max_intervals: int = Field(ge=1, le=128, strict=True)

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("residency_partition_method", canonical_trace(self))


class ResidencyPartitionReference(IntervalContract):
    reference: ResidencyRunReference
    closing_attestation_id: UUID
    expected_closing_attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_trace_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_method_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    partition_index: int = Field(ge=0, le=9223372036854775807, strict=True)


class ResidencyPartitionLimits(IntervalContract):
    max_source_messages: int = Field(default=8192, ge=2, le=8192, strict=True)
    max_manifest_records: int = Field(default=512, ge=1, le=512, strict=True)
    max_source_characters: int = Field(default=4194304, ge=1, le=4194304, strict=True)
    timeout_ms: int = Field(default=10000, ge=100, le=10000, strict=True)


class VerifiedResidencyPartition(IntervalContract):
    assessment_scope: Literal["source_verified_closed_residency_partition"] = (
        "source_verified_closed_residency_partition"
    )
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: ResidencyPartitionReference
    consumer_id: str = Field(min_length=1, max_length=128)
    parent_run: ResidencyRun = Field(repr=False)
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    method: ResidencyPartitionMethod
    partition_count: int = Field(ge=1, le=9223372036854775807, strict=True)
    after_interval: int = Field(ge=0, le=9223372036854775807, strict=True)
    through_interval: int = Field(ge=1, le=9223372036854775807, strict=True)
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

    @field_validator("read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_fixed_partition(self) -> "VerifiedResidencyPartition":
        run = self.parent_run
        candidate = self.candidate
        count = (run.intervals + self.method.max_intervals - 1) // self.method.max_intervals
        first, last = candidate.evidence[0].previous, candidate.evidence[-1].current
        source_keys = {(source.message_id, source.expected_seq) for source in self.sources}
        enough = (
            len(candidate.evidence) >= self.formation.minimum_intervals
            and candidate.observed_microseconds >= self.formation.minimum_duration_seconds * 1000000
        )
        if (
            self.request.reference.run_id != run.id
            or self.request.expected_method_fingerprint != self.method.fingerprint
            or self.partition_count != count
            or self.request.partition_index >= count
            or self.after_interval != self.request.partition_index * run.intervals // count
            or self.through_interval != (self.request.partition_index + 1) * run.intervals // count
            or len(candidate.evidence) != self.through_interval - self.after_interval
            or self.through_interval - self.after_interval > self.method.max_intervals
            or not enough
            or candidate.state != "stable_candidate"
            or (candidate.gpu_uuid, candidate.uid, candidate.username, candidate.model)
            != (run.gpu_uuid, run.uid, run.username, run.model)
            or run.rule_fingerprint != residency_fingerprint(self.continuity, self.formation)
            or not self.verified_first_seq <= first.seq < last.seq <= self.verified_last_seq
            or self.source_count != self.verified_last_seq - self.verified_first_seq + 1
            or (self.after_interval == 0 and first != run.first)
            or (self.through_interval == run.intervals and last != run.last)
            or first.compute_observed_at < run.first.compute_observed_at
            or last.compute_observed_at > run.last.compute_observed_at
            or first.compute_observed_at >= last.compute_observed_at
            or self.request.reference.source_cutoff > self.read_at
            or run.last.compute_observed_at > self.request.reference.source_cutoff
            or not (
                self.sources[0].message_id == first.message_id
                and self.sources[-1].message_id == last.message_id
            )
            or any(
                (source.agent_id, source.node_id) != (run.first.agent_id, run.first.node_id)
                or not first.seq <= source.expected_seq <= last.seq
                for source in self.sources
            )
            or len({source.message_id for source in self.sources}) != len(self.sources)
            or any(
                a.expected_seq >= b.expected_seq
                for a, b in zip(self.sources, self.sources[1:], strict=False)
            )
            or any(
                (ref.message_id, ref.seq) not in source_keys
                for edge in candidate.evidence
                for ref in (edge.previous, edge.current)
            )
            or self.fingerprint != partition_fingerprint(self)
        ):
            raise ValueError(
                "residency partition differs from its fixed closed parent and original window"
            )
        return self


def partition_fingerprint(value: VerifiedResidencyPartition) -> str:
    return trace_fingerprint(
        "verified_residency_partition", canonical_trace(value, exclude={"fingerprint", "read_at"})
    )
