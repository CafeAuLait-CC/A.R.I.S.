"""Complete sampled-window metrics are distinct from the recorded-only ledger report."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.source_prefix_schemas import SourcePrefixSelection, prefix_excludes_window
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import UsageArchiveHead

MAX_WINDOW_MANIFESTS = 4096
MAX_WINDOW_SOURCE_CELLS = 524288
MAX_WINDOW_SOURCE_BYTES = 256 * 1024 * 1024
MAX_WINDOW_CELLS = 8192
MICROSECOND = timedelta(microseconds=1)


class UsageWindowQuery(IntervalContract):
    node_id: str = Field(min_length=1, max_length=128)
    owner_id: UUID | None = None
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: str = Field(default="UTC", min_length=1, max_length=128)

    @field_validator("start_at", "end_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("timezone")
    @classmethod
    def calendar_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("usage window requires an IANA calendar timezone") from None
        return value

    @model_validator(mode="after")
    def bounded(self) -> "UsageWindowQuery":
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=366):
            raise ValueError("usage window requires a bounded positive interval")
        return self


class CoverageManifestReference(IntervalContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    attestation_id: UUID
    attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_seq: int = Field(ge=1)
    last_seq: int = Field(ge=1)


class WindowCoverageCell(IntervalContract):
    gpu_uuid: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    state: Literal["observed_idle", "recorded", "unknown", "uncovered"]
    reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    owner_id: UUID | None = None

    @property
    def microseconds(self) -> int:
        return (self.end_at - self.start_at) // MICROSECOND

    @model_validator(mode="after")
    def honest(self) -> "WindowCoverageCell":
        if self.start_at >= self.end_at or (self.state == "recorded") != (
            self.owner_id is not None
        ):
            raise ValueError("window cell requires a positive, explicitly attributed duration")
        return self


class SampledUsageWindow(SourcePrefixSelection):
    assessment_scope: Literal["committed_sampled_usage_window"] = "committed_sampled_usage_window"
    weighted: Literal[False] = False
    query: UsageWindowQuery
    configuration: ProcessingCoverageConfiguration
    source: UsageArchiveHead
    ledger: UsageLedgerHead
    identity_revision: int | None = Field(default=None, ge=1)
    manifests: tuple[CoverageManifestReference, ...] = Field(max_length=MAX_WINDOW_MANIFESTS)
    source_prefix_complete: bool = Field(strict=True)
    coverage: Literal["complete", "partial", "unknown"]
    quota_usage_available: bool = Field(strict=True)
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(default=None, ge=0)
    covered_gpu_microseconds: int = Field(ge=0)
    unknown_gpu_microseconds: int = Field(ge=0)
    uncovered_gpu_microseconds: int = Field(ge=0)
    cells: tuple[WindowCoverageCell, ...] = Field(max_length=MAX_WINDOW_CELLS)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_window(self) -> "SampledUsageWindow":
        position = {gpu: self.query.start_at for gpu in self.configuration.gpu_uuids}
        if tuple(sorted(self.cells, key=lambda cell: (cell.gpu_uuid, cell.start_at))) != self.cells:
            raise ValueError("window cells require canonical GPU/time order")
        for cell in self.cells:
            if (
                cell.gpu_uuid not in position
                or cell.start_at != position[cell.gpu_uuid]
                or cell.end_at > self.query.end_at
            ):
                raise ValueError("window cells must exactly partition every configured GPU")
            position[cell.gpu_uuid] = cell.end_at
        known = sum(
            cell.microseconds for cell in self.cells if cell.state in ("recorded", "observed_idle")
        )
        unknown = sum(cell.microseconds for cell in self.cells if cell.state == "unknown")
        uncovered = sum(cell.microseconds for cell in self.cells if cell.state == "uncovered")
        verified = sum(
            cell.microseconds
            for cell in self.cells
            if cell.state == "recorded"
            and (self.query.owner_id is None or self.query.owner_id == cell.owner_id)
        )
        complete = self.source_prefix_complete and unknown == uncovered == 0
        expected = "complete" if complete else ("partial" if known else "unknown")
        if (
            any(value != self.query.end_at for value in position.values())
            or self.query.node_id != self.configuration.trace.node_id
            or self.ledger.node_id != self.query.node_id
            or (self.source.query.agent_id, self.source.query.node_id)
            != (self.configuration.trace.agent_id, self.query.node_id)
            or self.read_at < max(self.query.end_at, self.source.read_at)
            or not prefix_excludes_window(
                self.prefix_checkpoint,
                configuration_fingerprint=self.configuration.trace.fingerprint,
                start_at=self.query.start_at,
                ceiling=self.source.highest_committed_seq,
                read_at=self.read_at,
            )
            or (known, unknown, uncovered, verified)
            != (
                self.covered_gpu_microseconds,
                self.unknown_gpu_microseconds,
                self.uncovered_gpu_microseconds,
                self.verified_usage_microseconds,
            )
            or self.coverage != expected
            or self.quota_usage_available != complete
            or self.total_usage_microseconds != (verified if complete else None)
            or self.verified_usage_microseconds > self.recorded_microseconds
            or self.fingerprint != usage_window_fingerprint(self)
            or len(canonical_trace(self).encode()) > 8 * 1024 * 1024
        ):
            raise ValueError("window totals, scope or completeness differ from captured evidence")
        return self


def usage_window_fingerprint(value: SampledUsageWindow) -> str:
    return trace_fingerprint(
        "sampled_usage_window", canonical_trace(value, exclude={"fingerprint"})
    )
