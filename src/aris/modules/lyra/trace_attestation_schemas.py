"""Bounded source attestations preserve history; they do not certify usage coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_ATTESTATION_BYTES = 16384


class TraceAttestationRequest(IntervalContract):
    id: UUID
    record_id: UUID
    expected_record_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_cutoff: AwareDatetime

    @field_validator("source_cutoff")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class StoredTraceAttestation(IntervalContract):
    assessment_scope: Literal["original_source_segment_attestation"] = (
        "original_source_segment_attestation"
    )
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: TraceAttestationRequest
    event_id: UUID
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    first_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    last_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    source_count: int = Field(ge=1, le=128, strict=True)
    telemetry_count: int = Field(ge=0, le=128, strict=True)
    previous_id: UUID | None
    previous_fingerprint: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent_segment(self) -> "StoredTraceAttestation":
        if (
            (self.revision == 1) != (self.previous_id is None)
            or (self.previous_id is None) != (self.previous_fingerprint is None)
            or self.previous_id == self.request.id
            or self.source_count != self.last_seq - self.first_seq + 1
            or self.telemetry_count > self.source_count
            or self.request.source_cutoff > self.created_at
            or self.fingerprint != attestation_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_ATTESTATION_BYTES
        ):
            raise ValueError("source attestation differs from its fixed bounded segment")
        return self


class TraceAttestationReceipt(IntervalContract):
    attestation: StoredTraceAttestation
    duplicate: bool = Field(strict=True)


def attestation_fingerprint(value: StoredTraceAttestation) -> str:
    return trace_fingerprint("attestation", canonical_trace(value, exclude={"fingerprint"}))
