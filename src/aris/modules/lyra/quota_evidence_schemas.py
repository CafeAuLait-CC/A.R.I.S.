"""Public historical accounting evidence, distinct from current quota authorization."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_reader_schemas import WindowPageCursor
from aris.modules.lyra.window_schemas import UsageWindowQuery

MAX_MODEL_MICROSECONDS = 128 * 366 * 86400 * 1000000


class QuotaEvidenceSelection(IntervalContract):
    snapshot_id: UUID
    expected_snapshot_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=128)
    owner_id: UUID


class QuotaEvidenceListQuery(UsageWindowQuery):
    owner_id: UUID
    before: WindowPageCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)


class QuotaEvidenceWindow(IntervalContract):
    selection: QuotaEvidenceSelection
    start_at: AwareDatetime
    end_at: AwareDatetime
    created_at: AwareDatetime

    @model_validator(mode="after")
    def positive(self) -> "QuotaEvidenceWindow":
        if not self.start_at < self.end_at <= self.created_at:
            raise ValueError("quota window metadata requires an observed historical interval")
        return self


class QuotaEvidencePage(IntervalContract):
    query: QuotaEvidenceListQuery
    items: tuple[QuotaEvidenceWindow, ...] = Field(max_length=16)
    next_cursor: WindowPageCursor | None = None


class HistoricalModelUsage(IntervalContract):
    model: str = Field(min_length=1, max_length=256)
    observed_microseconds: int = Field(gt=0, le=MAX_MODEL_MICROSECONDS, strict=True)


class HistoricalQuotaEvidence(IntervalContract):
    assessment_scope: Literal["historical_sampled_usage_evidence"] = (
        "historical_sampled_usage_evidence"
    )
    historical_only: Literal[True] = True
    current_quota_authorization: Literal[False] = False
    weighted: Literal[False] = False
    selection: QuotaEvidenceSelection
    window: UsageWindowQuery
    captured_at: AwareDatetime
    created_at: AwareDatetime
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_ceiling: int = Field(ge=0, strict=True)
    gpu_count: int = Field(ge=1, le=128, strict=True)
    ledger: UsageLedgerHead
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    recording_start_at: AwareDatetime
    identity_revision: int | None = Field(default=None, ge=1)
    coverage: Literal["complete", "partial", "unknown"]
    source_prefix_complete: bool = Field(strict=True)
    total_usage_microseconds: int | None = Field(ge=0, le=MAX_MODEL_MICROSECONDS, strict=True)
    models: tuple[HistoricalModelUsage, ...] | None = Field(max_length=4096)
    snapshot_window_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("captured_at", "created_at", "recording_start_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent(self) -> "HistoricalQuotaEvidence":
        complete = self.coverage == "complete"
        if (
            (self.selection.node_id, self.selection.owner_id)
            != (self.window.node_id, self.window.owner_id)
            or self.ledger.node_id != self.window.node_id
            or not self.window.end_at <= self.captured_at <= self.created_at
            or (
                complete
                and (
                    not self.source_prefix_complete
                    or self.recording_start_at > self.window.start_at
                )
            )
            or complete != (self.models is not None)
            or complete != (self.total_usage_microseconds is not None)
            or (
                self.models is not None
                and (
                    tuple(sorted(self.models, key=lambda item: item.model)) != self.models
                    or len({item.model for item in self.models}) != len(self.models)
                    or sum(item.observed_microseconds for item in self.models)
                    != self.total_usage_microseconds
                    or self.total_usage_microseconds
                    > self.gpu_count
                    * ((self.window.end_at - self.window.start_at) // timedelta(microseconds=1))
                )
            )
            or self.fingerprint != quota_evidence_fingerprint(self)
        ):
            raise ValueError("historical quota evidence differs from its fixed scope or total")
        return self


def quota_evidence_fingerprint(value: HistoricalQuotaEvidence) -> str:
    return trace_fingerprint(
        "historical_quota_evidence", canonical_trace(value, exclude={"fingerprint"})
    )
