"""Reproducible recorded time; no claim of complete source or quota coverage."""

from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from typing import Literal
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

MAX_REPORT_ENTRIES = 4096
MAX_REPORT_BYTES = 8 * 1024 * 1024
MICROSECOND = timedelta(microseconds=1)


class RecordedUsageQuery(IntervalContract):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    snapshot: UsageLedgerHead | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_window(self) -> "RecordedUsageQuery":
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=366) or (
            self.snapshot is not None and self.snapshot.node_id != self.node_id
        ):
            raise ValueError("recorded usage requires a bounded window and matching node")
        return self


class RecordedUsageContribution(IntervalContract):
    entry_id: UUID
    entry_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    gpu_uuid: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    original_start_at: AwareDatetime
    original_end_at: AwareDatetime
    start_at: AwareDatetime
    end_at: AwareDatetime
    recorded_microseconds: int = Field(gt=0, strict=True)
    timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_derivation_id: UUID
    source_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)

    @model_validator(mode="after")
    def exact_intersection(self) -> "RecordedUsageContribution":
        if not (
            self.original_start_at <= self.start_at < self.end_at <= self.original_end_at
            and self.recorded_microseconds == (self.end_at - self.start_at) // MICROSECOND
        ):
            raise ValueError("recorded contribution differs from its positive intersection")
        return self


class RecordedUsageMetrics(IntervalContract):
    assessment_scope: Literal["recorded_usage_only"] = "recorded_usage_only"
    method: Literal["recorded_residency_intersection"] = "recorded_residency_intersection"
    weighted: Literal[False] = False
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    reason: Literal["SOURCE_COVERAGE_NOT_ESTABLISHED"] = "SOURCE_COVERAGE_NOT_ESTABLISHED"
    node_id: str = Field(min_length=1, max_length=128)
    owner_id: UUID
    start_at: AwareDatetime
    end_at: AwareDatetime
    snapshot: UsageLedgerHead
    read_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0, strict=True)
    contributions: tuple[RecordedUsageContribution, ...] = Field(max_length=MAX_REPORT_ENTRIES)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fixed_report(self) -> "RecordedUsageMetrics":
        RecordedUsageQuery(
            node_id=self.node_id,
            start_at=self.start_at,
            end_at=self.end_at,
            snapshot=self.snapshot,
        )
        if (
            self.read_at < self.end_at
            or self.recorded_microseconds
            != sum(x.recorded_microseconds for x in self.contributions)
            or len({x.entry_id for x in self.contributions}) != len(self.contributions)
            or any(
                (item.start_at, item.end_at)
                != (
                    max(item.original_start_at, self.start_at),
                    min(item.original_end_at, self.end_at),
                )
                for item in self.contributions
            )
            or self.fingerprint
            != ledger_fingerprint(
                "recorded_metrics", canonical_ledger(self, exclude={"fingerprint", "read_at"})
            )
        ):
            raise ValueError("recorded usage differs from its scope, contributions or fingerprint")
        return self


def _active_prefix(
    head: UsageLedgerHead, entries: tuple[UsageLedgerEntry, ...], at: datetime
) -> tuple[UsageLedgerEntry, ...]:
    """Verify the entire bounded logical prefix, including past adjustments."""
    if len(entries) != head.revision or len(entries) > MAX_REPORT_ENTRIES:
        raise ValueError("ledger prefix is incomplete or exceeds the report budget")
    previous: str | None = None
    last_at: datetime | None = None
    by_id: dict[UUID, UsageLedgerEntry] = {}
    active: dict[UUID, UsageLedgerEntry] = {}
    keys: set[str] = set()
    windows: dict[str, list[tuple[datetime, str]]] = {}
    encoded_size = 0
    for revision, entry in enumerate(entries, start=1):
        entry = UsageLedgerEntry.model_validate(entry.model_dump(mode="json"))
        encoded_size += len(canonical_ledger(entry).encode())
        if (
            encoded_size > MAX_REPORT_BYTES
            or entry.node_id != head.node_id
            or entry.revision != revision
            or entry.previous_fingerprint != previous
            or entry.id in by_id
            or entry.recorded_at > at
            or (last_at is not None and entry.recorded_at < last_at)
        ):
            raise ValueError("ledger prefix chain is inconsistent")
        window = entry.details.measurement
        gpu = windows.setdefault(window.gpu_uuid, [])
        position = bisect_left(gpu, (window.start_at, str(entry.id)))
        if isinstance(entry.details, PostedMeasurement):
            key = measurement_key(window)
            if key in keys:
                raise ValueError("ledger prefix repeats an original measured window")
            for neighbor in gpu[max(0, position - 1) : position + 1]:
                other = active[UUID(neighbor[1])].details.measurement
                if other.start_at < window.end_at and other.end_at > window.start_at:
                    raise ValueError("ledger prefix contains overlapping active GPU time")
            keys.add(key)
            gpu.insert(position, (window.start_at, str(entry.id)))
            active[entry.id] = entry
        else:
            assert isinstance(entry.details, ReversedMeasurement)
            target = active.get(entry.details.target_id)
            if (
                target is None
                or target.fingerprint != entry.details.target_fingerprint
                or MeasuredWindow.model_validate(
                    target.details.measurement.model_dump(include=set(MeasuredWindow.model_fields))
                )
                != window
            ):
                raise ValueError("ledger reversal does not name an active original measurement")
            gpu.remove((window.start_at, str(target.id)))
            del active[target.id]
        by_id[entry.id] = entry
        previous, last_at = entry.fingerprint, entry.recorded_at
    if previous != head.fingerprint:
        raise ValueError("ledger prefix does not match its committed head")
    return tuple(active.values())


def recorded_usage_metrics(
    query: RecordedUsageQuery,
    *,
    owner_id: UUID,
    head: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    read_at: datetime,
) -> RecordedUsageMetrics:
    """Pure reduction. The service must authenticate and load the fixed prefix."""
    query = RecordedUsageQuery.model_validate(query.model_dump(mode="json"))
    head = UsageLedgerHead.model_validate(head.model_dump(mode="json"))
    if read_at.tzinfo is None or read_at.utcoffset() is None:
        raise ValueError("recorded usage time requires a timezone")
    read_at = read_at.astimezone(UTC)
    if head.node_id != query.node_id or (query.snapshot is not None and query.snapshot != head):
        raise ValueError("recorded usage does not match the requested ledger prefix")
    contributions = []
    for entry in _active_prefix(head, entries, read_at):
        assert isinstance(entry.details, PostedMeasurement)
        value = entry.details.measurement
        start, end = max(value.start_at, query.start_at), min(value.end_at, query.end_at)
        if value.owner_id != owner_id or end <= start:
            continue
        contributions.append(
            RecordedUsageContribution(
                entry_id=entry.id,
                entry_fingerprint=entry.fingerprint,
                gpu_uuid=value.gpu_uuid,
                model=value.model,
                original_start_at=value.start_at,
                original_end_at=value.end_at,
                start_at=start,
                end_at=end,
                recorded_microseconds=(end - start) // MICROSECOND,
                timing_fingerprint=value.timing_fingerprint,
                configuration_fingerprint=entry.details.configuration.fingerprint,
                source_derivation_id=value.source_derivation_id,
                source_result_fingerprint=value.source_result_fingerprint,
                identity_revision=value.identity_revision,
            )
        )
    ordered = tuple(sorted(contributions, key=lambda x: (x.start_at, x.gpu_uuid, str(x.entry_id))))
    result = RecordedUsageMetrics.model_construct(
        node_id=query.node_id,
        owner_id=owner_id,
        start_at=query.start_at,
        end_at=query.end_at,
        snapshot=head,
        read_at=read_at,
        recorded_microseconds=sum(x.recorded_microseconds for x in ordered),
        contributions=ordered,
        fingerprint="0" * 64,
    )
    return RecordedUsageMetrics.model_validate(
        {
            **result.model_dump(mode="json"),
            "fingerprint": ledger_fingerprint(
                "recorded_metrics", canonical_ledger(result, exclude={"fingerprint", "read_at"})
            ),
        }
    )
