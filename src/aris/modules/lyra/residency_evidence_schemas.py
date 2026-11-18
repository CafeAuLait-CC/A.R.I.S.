"""Original-source verification of one sampled candidate, never a quota ticket."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.residency_sessions import ResidencyFormationRule, residency_fingerprint
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import IntervalContract


class ResidencyRunReference(IntervalContract):
    record_id: UUID
    expected_record_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_cutoff: AwareDatetime

    @field_validator("source_cutoff")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ResidencyVerificationLimits(IntervalContract):
    max_source_messages: int = Field(default=4096, ge=2, le=16384, strict=True)
    max_records: int = Field(default=512, ge=1, le=4096, strict=True)
    timeout_ms: int = Field(default=10000, ge=100, le=10000, strict=True)


class VerifiedResidencyWindow(IntervalContract):
    assessment_scope: Literal["source_verified_residency_candidate"] = (
        "source_verified_residency_candidate"
    )
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    platform_owner_id: None = None
    total_usage_microseconds: None = None
    usage_coverage: Literal["not_assessed"] = "not_assessed"
    reference: ResidencyRunReference
    consumer_id: str = Field(min_length=1, max_length=128)
    consumer_revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_created_at: AwareDatetime
    run: ResidencyRun
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    candidate_state: Literal["stable_candidate", "insufficient_evidence"]
    state_at_record: Literal["open", "closed"]
    verified_through_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    source_count: int = Field(ge=2, le=16384, strict=True)
    telemetry_count: int = Field(ge=2, le=16384, strict=True)
    manifest_records: int = Field(ge=1, le=4096, strict=True)
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("record_created_at", "read_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_verified_window(self) -> "VerifiedResidencyWindow":
        enough = (
            self.run.intervals >= self.formation.minimum_intervals
            and self.run.observed_microseconds >= self.formation.minimum_duration_seconds * 1000000
        )
        if (
            self.run.id != self.reference.run_id
            or self.run.rule_fingerprint != residency_fingerprint(self.continuity, self.formation)
            or (self.candidate_state == "stable_candidate") != enough
            or not self.record_created_at <= self.reference.source_cutoff <= self.read_at
            or self.run.last.compute_observed_at > self.reference.source_cutoff
            or self.verified_through_seq < self.run.last.seq
            or self.source_count != self.verified_through_seq - self.run.first.seq + 1
            or self.telemetry_count > self.source_count
            or self.run.intervals >= self.telemetry_count
            or self.fingerprint != residency_evidence_fingerprint(self)
        ):
            raise ValueError("residency proof differs from its fixed source window or fingerprint")
        return self


def residency_evidence_fingerprint(value: VerifiedResidencyWindow) -> str:
    raw = json.dumps(
        value.model_dump(mode="json", exclude={"fingerprint", "read_at"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.lyra.residency.evidence\x00" + raw.encode()).hexdigest()
