"""Immutable captured metrics refer to a fixed set of committed coverage manifests."""

from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.source_prefix_schemas import SourcePrefixSelection
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_schemas import (
    MAX_WINDOW_MANIFESTS,
    SampledUsageWindow,
    UsageWindowQuery,
)

MAX_WINDOW_SNAPSHOT_BYTES = 8 * 1024 * 1024


class WindowManifestSelection(IntervalContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class CaptureUsageWindow(SourcePrefixSelection):
    id: UUID
    query: UsageWindowQuery
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    ledger: UsageLedgerHead
    manifests: tuple[WindowManifestSelection, ...] = Field(max_length=MAX_WINDOW_MANIFESTS)

    @field_validator("manifests")
    @classmethod
    def unique_sources(
        cls, value: tuple[WindowManifestSelection, ...]
    ) -> tuple[WindowManifestSelection, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("each selected manifest must appear once")
        return tuple(sorted(value, key=lambda item: str(item.id)))

    @model_validator(mode="after")
    def one_node(self) -> "CaptureUsageWindow":
        if self.ledger.node_id != self.query.node_id:
            raise ValueError("window capture requires one node")
        return self


class StoredUsageWindow(IntervalContract):
    request: CaptureUsageWindow
    window: SampledUsageWindow
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact(self) -> "StoredUsageWindow":
        request, result = self.request, self.window
        if (
            request.query != result.query
            or request.prefix_checkpoint != result.prefix_checkpoint
            or request.ledger != result.ledger
            or request.expected_source_fingerprint != result.source.fingerprint
            or request.expected_configuration_fingerprint != result.configuration.fingerprint
            or {(item.id, item.fingerprint) for item in request.manifests}
            != {(item.id, item.fingerprint) for item in result.manifests}
            or self.created_at < result.read_at
            or self.fingerprint != window_snapshot_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_WINDOW_SNAPSHOT_BYTES
        ):
            raise ValueError("captured window differs from its fixed request or evidence")
        return self


class UsageWindowReceipt(IntervalContract):
    snapshot: StoredUsageWindow
    duplicate: bool = Field(strict=True)


def window_snapshot_fingerprint(value: StoredUsageWindow) -> str:
    return trace_fingerprint(
        "usage_window_snapshot", canonical_trace(value, exclude={"fingerprint"})
    )
