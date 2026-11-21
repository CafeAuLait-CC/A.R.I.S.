"""Previously verified ledger chain segments, distinct from usage and quota coverage."""

from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_plan import MeasuredWindow
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    ReversedMeasurement,
    UsageLedgerEntry,
    UsageLedgerHead,
    canonical_ledger,
    ledger_fingerprint,
    measurement_key,
)
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_LEDGER_SEGMENT_ENTRIES = 256
MAX_LEDGER_SEGMENT_BYTES = 262144
MAX_LEDGER_SEGMENT_SOURCE_BYTES = 8 * 1024 * 1024


class LedgerSegmentConfiguration(IntervalContract):
    method: Literal["previously_verified_original_ledger_chain"] = (
        "previously_verified_original_ledger_chain"
    )
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    source_ref: str = Field(min_length=1, max_length=128)
    max_entries: int = Field(default=256, ge=1, le=256, strict=True)

    @property
    def fingerprint(self) -> str:
        return ledger_fingerprint("segment_configuration", canonical_ledger(self))


class LedgerEntryIndex(IntervalContract):
    entry_id: UUID
    revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    recorded_at: AwareDatetime
    kind: Literal["measurement", "reversal"]
    gpu_uuid: str = Field(min_length=1, max_length=128)
    owner_id: UUID
    model: str = Field(min_length=1, max_length=256)
    start_at: AwareDatetime
    end_at: AwareDatetime
    timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_id: UUID | None = None
    target_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("recorded_at", "start_at", "end_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def indexed_kind(self) -> "LedgerEntryIndex":
        if (
            self.start_at >= self.end_at
            or self.recorded_at < self.end_at
            or (self.revision == 1) != (self.previous_fingerprint is None)
            or (self.kind == "measurement") != (self.target_id is None)
            or (self.target_id is None) != (self.target_fingerprint is None)
            or self.target_id == self.entry_id
        ):
            raise ValueError("ledger index differs from its entry kind, time or chain")
        return self

    def window(self, node_id: str) -> MeasuredWindow:
        value = MeasuredWindow(
            node_id=node_id,
            gpu_uuid=self.gpu_uuid,
            owner_id=self.owner_id,
            model=self.model,
            start_at=self.start_at,
            end_at=self.end_at,
            observed_microseconds=(self.end_at - self.start_at) // timedelta(microseconds=1),
            timing_fingerprint=self.timing_fingerprint,
        )
        if measurement_key(value) != self.window_fingerprint:
            raise ValueError("ledger index does not bind its original measured window")
        return value


def ledger_entry_index(entry: UsageLedgerEntry) -> LedgerEntryIndex:
    entry = UsageLedgerEntry.model_validate(entry.model_dump(mode="json"))
    window = entry.details.measurement
    reversal = entry.details if isinstance(entry.details, ReversedMeasurement) else None
    assert reversal is not None or isinstance(entry.details, PostedMeasurement)
    return LedgerEntryIndex(
        entry_id=entry.id,
        revision=entry.revision,
        fingerprint=entry.fingerprint,
        previous_fingerprint=entry.previous_fingerprint,
        recorded_at=entry.recorded_at,
        kind=entry.details.kind,
        gpu_uuid=window.gpu_uuid,
        owner_id=window.owner_id,
        model=window.model,
        start_at=window.start_at,
        end_at=window.end_at,
        timing_fingerprint=window.timing_fingerprint,
        window_fingerprint=measurement_key(window),
        target_id=reversal.target_id if reversal else None,
        target_fingerprint=reversal.target_fingerprint if reversal else None,
    )


class LedgerSegmentReference(IntervalContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    head: UsageLedgerHead
    last_recorded_at: AwareDatetime
    created_at: AwareDatetime

    @model_validator(mode="after")
    def nonempty_anchor(self) -> "LedgerSegmentReference":
        if self.head.revision == 0 or self.created_at < self.last_recorded_at:
            raise ValueError("ledger segment anchor is empty or predates its entries")
        return self


class LedgerRangeBoundary(Protocol):
    @property
    def target(self) -> UsageLedgerHead: ...

    @property
    def previous(self) -> LedgerSegmentReference | None: ...


class CaptureLedgerSegment(IntervalContract):
    id: UUID
    target: UsageLedgerHead
    previous: LedgerSegmentReference | None = None
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class StoredLedgerSegment(IntervalContract):
    verification_scope: Literal["previously_verified_original_ledger_chain"] = (
        "previously_verified_original_ledger_chain"
    )
    quota_usage_available: Literal[False] = False
    request: CaptureLedgerSegment
    configuration: LedgerSegmentConfiguration
    entries: tuple[LedgerEntryIndex, ...] = Field(min_length=1, max_length=256)
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    def reference(self) -> LedgerSegmentReference:
        return LedgerSegmentReference(
            id=self.request.id,
            fingerprint=self.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            head=self.request.target,
            last_recorded_at=self.entries[-1].recorded_at,
            created_at=self.created_at,
        )

    @model_validator(mode="after")
    def fixed_segment(self) -> "StoredLedgerSegment":
        request, config = self.request, self.configuration
        previous = request.previous
        revision = previous.head.revision if previous else 0
        fingerprint = previous.head.fingerprint if previous else None
        at = previous.last_recorded_at if previous else None
        if (
            request.target.node_id != config.node_id
            or request.expected_configuration_fingerprint != config.fingerprint
            or len(self.entries) > config.max_entries
            or request.target.revision - revision != len(self.entries)
            or (
                previous is not None
                and (
                    previous.head.node_id != config.node_id
                    or previous.configuration_fingerprint != config.fingerprint
                    or previous.created_at > self.created_at
                )
            )
        ):
            raise ValueError("ledger segment differs from its configuration and anchored range")
        seen: set[UUID] = set()
        for entry in self.entries:
            revision += 1
            if (
                entry.revision != revision
                or entry.previous_fingerprint != fingerprint
                or (at is not None and entry.recorded_at < at)
                or entry.recorded_at > self.created_at
                or entry.entry_id in seen
            ):
                raise ValueError("ledger segment index chain is inconsistent")
            entry.window(config.node_id)
            seen.add(entry.entry_id)
            fingerprint, at = entry.fingerprint, entry.recorded_at
        if (
            request.target.fingerprint != fingerprint
            or self.fingerprint != ledger_segment_fingerprint(self)
            or len(canonical_ledger(self).encode()) > MAX_LEDGER_SEGMENT_BYTES
        ):
            raise ValueError("ledger segment does not bind its fixed head and fingerprint")
        return self


def ledger_segment_fingerprint(value: StoredLedgerSegment) -> str:
    return ledger_fingerprint("ledger_segment", canonical_ledger(value, exclude={"fingerprint"}))
