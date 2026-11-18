"""Fixed measured-time entries; a receipt never asserts complete quota coverage."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from aris.modules.lyra.ledger_plan import (
    LedgerPlanningRules,
    MeasuredWindow,
    UsageMeasurementProposal,
)
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_LEDGER_BYTES = 65536
MAX_LEDGER_REVISION = 9223372036854775807


def canonical_ledger(value: IntervalContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def ledger_fingerprint(kind: str, value: str) -> str:
    return hashlib.sha256(
        b"aris.lyra.ledger." + kind.encode() + b"\x00" + value.encode()
    ).hexdigest()


def measurement_key(value: MeasuredWindow) -> str:
    window = MeasuredWindow.model_validate(
        value.model_dump(include=set(MeasuredWindow.model_fields))
    )
    return ledger_fingerprint("measurement", canonical_ledger(window))


class UsagePostingConfiguration(IntervalContract):
    """Trusted composition input; not an external approval or a caller permission."""

    source_ref: str = Field(min_length=1, max_length=128)
    recording_start_at: AwareDatetime
    timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("recording_start_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def fingerprint(self) -> str:
        return ledger_fingerprint("configuration", canonical_ledger(self))

    def planning_rules(self) -> LedgerPlanningRules:
        return LedgerPlanningRules(
            recording_start_at=self.recording_start_at,
            expected_timing_fingerprint=self.timing_fingerprint,
        )


class UsagePostingContext(IntervalContract):
    actor: Literal["system:lyra"] = "system:lyra"
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class UsagePostingRequest(IntervalContract):
    operation: Literal["post_measurement"] = "post_measurement"
    id: UUID
    derivation_id: UUID
    session_index: int = Field(ge=0, lt=128, strict=True)
    expected_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class UsageReversalRequest(IntervalContract):
    operation: Literal["reverse_measurement"] = "reverse_measurement"
    id: UUID
    target_id: UUID
    expected_target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_ledger_revision: int = Field(ge=1, le=MAX_LEDGER_REVISION, strict=True)
    reason: str = Field(min_length=1, max_length=256, repr=False)


UsageOperationRequest = Annotated[
    UsagePostingRequest | UsageReversalRequest, Field(discriminator="operation")
]
operation_request_adapter: TypeAdapter[UsagePostingRequest | UsageReversalRequest] = TypeAdapter(
    UsageOperationRequest
)


class PostedMeasurement(IntervalContract):
    kind: Literal["measurement"] = "measurement"
    measurement: UsageMeasurementProposal
    configuration: UsagePostingConfiguration

    @model_validator(mode="after")
    def configured_window(self) -> "PostedMeasurement":
        if (
            self.measurement.timing_fingerprint != self.configuration.timing_fingerprint
            or self.measurement.start_at < self.configuration.recording_start_at
        ):
            raise ValueError("measurement differs from its fixed recording context")
        return self


class ReversedMeasurement(IntervalContract):
    kind: Literal["reversal"] = "reversal"
    measurement: MeasuredWindow
    target_id: UUID
    target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=256, repr=False)


class UsageLedgerEntry(IntervalContract):
    assessment_scope: Literal["recorded_usage_measurement"] = "recorded_usage_measurement"
    quota_usage_available: Literal[False] = False
    id: UUID
    event_id: UUID
    node_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, le=MAX_LEDGER_REVISION, strict=True)
    previous_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    actor: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    recorded_at: AwareDatetime
    details: Annotated[PostedMeasurement | ReversedMeasurement, Field(discriminator="kind")]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("recorded_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fixed_entry(self) -> "UsageLedgerEntry":
        raw = canonical_ledger(self, exclude={"fingerprint"})
        if (
            self.node_id != self.details.measurement.node_id
            or (self.revision == 1) != (self.previous_fingerprint is None)
            or self.recorded_at < self.details.measurement.end_at
            or len(raw.encode()) > MAX_LEDGER_BYTES
            or self.fingerprint != ledger_fingerprint("entry", raw)
            or (isinstance(self.details, ReversedMeasurement) and self.details.target_id == self.id)
        ):
            raise ValueError("ledger entry differs from its window, chain or fingerprint")
        return self


class UsageLedgerHead(IntervalContract):
    node_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=0, le=MAX_LEDGER_REVISION, strict=True)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def empty_or_fixed(self) -> "UsageLedgerHead":
        if (self.revision == 0) != (self.fingerprint is None):
            raise ValueError("ledger head requires an exact committed prefix")
        return self


class UsageLedgerReceipt(IntervalContract):
    assessment_scope: Literal["usage_ledger_receipt"] = "usage_ledger_receipt"
    quota_usage_available: Literal[False] = False
    actor: str = Field(min_length=1, max_length=128)
    id: UUID
    operation: Literal["post_measurement", "reverse_measurement"]
    duplicate: bool = Field(strict=True)
    effect: Literal["recorded", "already_recorded", "reversed"]
    created_at: AwareDatetime
    head: UsageLedgerHead
    entries: tuple[UUID, ...] = Field(min_length=1, max_length=17)
    entry_fingerprints: tuple[str, ...] = Field(min_length=1, max_length=17)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_result(self) -> "UsageLedgerReceipt":
        if (
            self.head.revision == 0
            or len(self.entries) != len(self.entry_fingerprints)
            or len(set(self.entries)) != len(self.entries)
            or any(
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                for value in self.entry_fingerprints
            )
            or (self.operation == "reverse_measurement") != (self.effect == "reversed")
            or (self.operation == "post_measurement" and len(self.entries) != 1)
        ):
            raise ValueError("ledger receipt differs from its operation result")
        return self


class StoredUsageOperation(IntervalContract):
    request: UsageOperationRequest
    receipt: UsageLedgerReceipt

    @model_validator(mode="after")
    def exact_operation(self) -> "StoredUsageOperation":
        if (
            self.request.id != self.receipt.id
            or self.request.operation != self.receipt.operation
            or self.receipt.request_fingerprint
            != ledger_fingerprint("request", canonical_ledger(self.request))
        ):
            raise ValueError("stored ledger operation differs from its request")
        return self


def make_ledger_entry(
    *,
    id: UUID,
    event_id: UUID,
    node_id: str,
    revision: int,
    previous_fingerprint: str | None,
    actor: str,
    request_id: str,
    recorded_at: datetime,
    details: PostedMeasurement | ReversedMeasurement,
) -> UsageLedgerEntry:
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("usage ledger entry time requires a timezone")
    unchecked = UsageLedgerEntry.model_construct(
        id=id,
        event_id=event_id,
        node_id=node_id,
        revision=revision,
        previous_fingerprint=previous_fingerprint,
        actor=actor,
        request_id=request_id,
        recorded_at=recorded_at.astimezone(UTC),
        details=details,
        fingerprint="0" * 64,
    )
    raw = canonical_ledger(unchecked, exclude={"fingerprint"})
    fixed = unchecked.model_copy(update={"fingerprint": ledger_fingerprint("entry", raw)})
    return UsageLedgerEntry.model_validate(fixed.model_dump(mode="json"))
