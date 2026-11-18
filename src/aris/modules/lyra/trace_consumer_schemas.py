"""Durable candidate progress is separate from sessions, charges and quota coverage."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.trace_state import (
    ObservationStamp,
    UsageContinuityRule,
    UsageTraceCheckpoint,
)
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_MESSAGES,
    MAX_ARCHIVE_SEQ,
    ArchivedUsageReference,
)

MAX_TRACE_RECORD_BYTES = 262144


def canonical_trace(value: IntervalContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def trace_fingerprint(kind: str, raw: str) -> str:
    return hashlib.sha256(
        b"aris.lyra.trace.consumer." + kind.encode() + b"\x00" + raw.encode()
    ).hexdigest()


class UsageTraceConfiguration(IntervalContract):
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    source_ref: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    initial_after_seq: int = Field(ge=0, lt=MAX_ARCHIVE_SEQ, strict=True)
    continuity: UsageContinuityRule

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("configuration", canonical_trace(self))


class UsageTraceConsumeRequest(IntervalContract):
    id: UUID
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    through_seq: int = Field(ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    limit: int = Field(default=128, ge=1, le=MAX_ARCHIVE_MESSAGES, strict=True)


class UsageTraceConsumeContext(IntervalContract):
    actor: Literal["system:lyra"] = "system:lyra"
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class UsageTracePosition(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    configuration: UsageTraceConfiguration
    revision: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    after_seq: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    trace: UsageTraceCheckpoint | None = None
    record_id: UUID | None = None
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    recorded_at: AwareDatetime | None = None

    @field_validator("recorded_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def exact_position(self) -> "UsageTracePosition":
        initial = self.revision == 0
        if (
            initial != (self.record_id is None)
            or initial != (self.fingerprint is None)
            or initial != (self.recorded_at is None)
            or (
                initial
                and (
                    self.after_seq != self.configuration.initial_after_seq or self.trace is not None
                )
            )
            or (not initial and self.after_seq <= self.configuration.initial_after_seq)
            or (
                self.trace is not None
                and (
                    self.trace.rule != self.configuration.continuity
                    or self.trace.last.reference.seq > self.after_seq
                    or self.trace.anchor.reference.seq <= self.configuration.initial_after_seq
                    or (self.trace.last.reference.agent_id, self.trace.last.reference.node_id)
                    != (self.configuration.agent_id, self.configuration.node_id)
                )
            )
        ):
            raise ValueError("trace position differs from its configured source and revision")
        return self


class ConsumedTraceSource(IntervalContract):
    reference: ArchivedUsageReference
    observation: ObservationStamp | None = None

    @model_validator(mode="after")
    def exact_source_stamp(self) -> "ConsumedTraceSource":
        ref, stamp = self.reference, self.observation
        if (ref.kind == "telemetry") != (stamp is not None):
            raise ValueError("only telemetry can contain a consumed observation stamp")
        if stamp is not None:
            value = stamp.reference
            if (value.agent_id, value.node_id, value.message_id, value.seq, value.fingerprint) != (
                ref.agent_id,
                ref.node_id,
                ref.message_id,
                ref.seq,
                ref.fingerprint,
            ) or value.compute_observed_at > ref.observed_at:
                raise ValueError("consumed observation stamp differs from its archived source")
        return self


class StoredTraceConsumption(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    request: UsageTraceConsumeRequest
    context: UsageTraceConsumeContext
    event_id: UUID
    before: UsageTracePosition
    sources: tuple[ConsumedTraceSource, ...] = Field(min_length=1, max_length=MAX_ARCHIVE_MESSAGES)
    after_trace: UsageTraceCheckpoint | None
    transitions_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_state: Literal["range_end", "page_end", "gap"]
    missing_seq: int | None = Field(default=None, ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def position(self) -> UsageTracePosition:
        return UsageTracePosition(
            configuration=self.before.configuration,
            revision=self.before.revision + 1,
            after_seq=self.sources[-1].reference.seq,
            trace=self.after_trace,
            record_id=self.request.id,
            fingerprint=self.fingerprint,
            recorded_at=self.created_at,
        )

    @model_validator(mode="after")
    def coherent_consumption(self) -> "StoredTraceConsumption":
        config, request = self.before.configuration, self.request
        last = self.sources[-1].reference.seq
        expected_state = (
            "range_end"
            if last == request.through_seq
            else ("page_end" if len(self.sources) == request.limit else "gap")
        )
        observations = tuple(
            item.observation for item in self.sources if item.observation is not None
        )
        if (
            request.expected_configuration_fingerprint != config.fingerprint
            or request.expected_revision != self.before.revision
            or len(self.sources) > request.limit
            or last > request.through_seq
            or self.page_state != expected_state
            or self.missing_seq != (last + 1 if expected_state == "gap" else None)
            or self.request.id == self.before.record_id
            or (self.before.recorded_at is not None and self.created_at < self.before.recorded_at)
            or any(
                (item.reference.agent_id, item.reference.node_id, item.reference.seq)
                != (config.agent_id, config.node_id, self.before.after_seq + index)
                or item.reference.received_at > self.created_at
                for index, item in enumerate(self.sources, start=1)
            )
            or len({item.reference.message_id for item in self.sources}) != len(self.sources)
            or (bool(observations) and self.after_trace is None)
            or (not observations and self.after_trace != self.before.trace)
            or (
                observations
                and self.after_trace is not None
                and self.after_trace.last != observations[-1]
            )
            or len(canonical_trace(self).encode()) > MAX_TRACE_RECORD_BYTES
            or self.fingerprint
            != trace_fingerprint("record", canonical_trace(self, exclude={"fingerprint"}))
        ):
            raise ValueError("trace consumption differs from its original contiguous source prefix")
        _ = self.position
        return self


class TraceConsumptionReceipt(IntervalContract):
    recorded: Literal[True] = True
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    id: UUID
    duplicate: bool = Field(strict=True)
    event_id: UUID
    position: UsageTracePosition
    source_count: int = Field(ge=1, le=MAX_ARCHIVE_MESSAGES, strict=True)
    telemetry_count: int = Field(ge=0, le=MAX_ARCHIVE_MESSAGES, strict=True)
    page_state: Literal["range_end", "page_end", "gap"]
    missing_seq: int | None

    @model_validator(mode="after")
    def bound_receipt(self) -> "TraceConsumptionReceipt":
        if (
            self.position.record_id != self.id
            or self.position.revision == 0
            or self.telemetry_count > self.source_count
            or (self.page_state == "gap") != (self.missing_seq is not None)
            or (self.missing_seq is not None and self.missing_seq != self.position.after_seq + 1)
        ):
            raise ValueError("trace receipt differs from its recorded progress")
        return self


class TraceConsumptionIdle(IntervalContract):
    recorded: Literal[False] = False
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    id: UUID
    state: Literal["gap", "caught_up"]
    position: UsageTracePosition
    missing_seq: int | None

    @model_validator(mode="after")
    def no_progress(self) -> "TraceConsumptionIdle":
        if self.missing_seq != (self.position.after_seq + 1 if self.state == "gap" else None):
            raise ValueError("no-progress result differs from its unchanged archive position")
        return self


def consumption_receipt(
    record: StoredTraceConsumption, *, duplicate: bool
) -> TraceConsumptionReceipt:
    return TraceConsumptionReceipt(
        id=record.request.id,
        duplicate=duplicate,
        event_id=record.event_id,
        position=record.position,
        source_count=len(record.sources),
        telemetry_count=sum(item.observation is not None for item in record.sources),
        page_state=record.page_state,
        missing_seq=record.missing_seq,
    )
