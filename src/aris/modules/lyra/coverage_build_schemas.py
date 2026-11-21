"""An admitted fixed boundary and bounded immutable coverage build progress."""

from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.source_prefix_schemas import SourcePrefixSelection, prefix_excludes_window
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_schemas import MAX_WINDOW_MANIFESTS, UsageWindowQuery
from aris.modules.lyra.window_snapshot_schemas import WindowManifestSelection
from aris.modules.vega.usage_archive_schemas import UsageArchiveHead

MAX_BUILD_PLAN_BYTES = 65536
MAX_BUILD_STEP_BYTES = 16384


class CreateCoverageBuild(SourcePrefixSelection):
    id: UUID
    query: UsageWindowQuery
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    ledger: UsageLedgerHead


class AdmitCoverageWindow(SourcePrefixSelection):
    id: UUID
    query: UsageWindowQuery
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class StoredCoverageBuild(IntervalContract):
    request: CreateCoverageBuild
    configuration: ProcessingCoverageConfiguration
    source: UsageArchiveHead
    tail: StoredTraceAttestation | None
    identity_revision: int = Field(ge=1, strict=True)
    recording_revision: int = Field(ge=0, strict=True)
    read_at: AwareDatetime
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def start_revision(self) -> int:
        prefix = self.request.prefix_checkpoint
        return prefix.through_revision if prefix else 0

    @property
    def target_revision(self) -> int:
        return self.tail.revision if self.tail else 0

    @property
    def window_id(self) -> UUID:
        return uuid5(self.request.id, "completed-coverage-window")

    def manifest_id(self, revision: int) -> UUID:
        return uuid5(self.request.id, "coverage-source:" + str(revision))

    @model_validator(mode="after")
    def admitted_boundaries(self) -> "StoredCoverageBuild":
        request, config = self.request, self.configuration
        if (
            request.expected_configuration_fingerprint != config.fingerprint
            or request.expected_source_fingerprint != self.source.fingerprint
            or request.query.node_id != config.trace.node_id
            or request.ledger.node_id != config.trace.node_id
            or (self.source.query.agent_id, self.source.query.node_id)
            != (config.trace.agent_id, config.trace.node_id)
            or self.read_at < max(request.query.end_at, self.source.read_at)
            or self.created_at < self.read_at
            or not prefix_excludes_window(
                request.prefix_checkpoint,
                configuration_fingerprint=config.trace.fingerprint,
                start_at=request.query.start_at,
                ceiling=self.source.highest_committed_seq,
                read_at=self.read_at,
            )
            or not 0 <= self.target_revision - self.start_revision <= MAX_WINDOW_MANIFESTS
            or (self.tail is None)
            != (self.source.highest_committed_seq == config.trace.initial_after_seq)
            or (
                self.tail is not None
                and (
                    self.tail.consumer_id != config.trace.consumer_id
                    or self.tail.configuration_fingerprint != config.trace.fingerprint
                    or self.tail.last_seq != self.source.highest_committed_seq
                    or self.tail.created_at > self.read_at
                )
            )
            or self.fingerprint != coverage_build_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_BUILD_PLAN_BYTES
        ):
            raise ValueError("coverage build differs from its admitted fixed boundaries")
        return self


def coverage_build_fingerprint(value: StoredCoverageBuild) -> str:
    return trace_fingerprint("coverage_build", canonical_trace(value, exclude={"fingerprint"}))


class AdvanceCoverageBuild(IntervalContract):
    id: UUID
    expected_plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=0, lt=9223372036854775807, strict=True)


class StoredCoverageBuildStep(IntervalContract):
    plan_id: UUID
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    previous_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    attestation_id: UUID
    attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_seq: int = Field(ge=1, strict=True)
    last_seq: int = Field(ge=1, strict=True)
    manifest: WindowManifestSelection
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fixed_step(self) -> "StoredCoverageBuildStep":
        if (
            self.first_seq > self.last_seq
            or self.fingerprint != coverage_build_step_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_BUILD_STEP_BYTES
        ):
            raise ValueError("coverage build step differs from its immutable proof binding")
        return self


def coverage_build_step_fingerprint(value: StoredCoverageBuildStep) -> str:
    return trace_fingerprint("coverage_build_step", canonical_trace(value, exclude={"fingerprint"}))


class CoverageBuildProgress(IntervalContract):
    plan_id: UUID
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    processed_revision: int = Field(ge=0, le=9223372036854775807, strict=True)
    target_revision: int = Field(ge=0, le=9223372036854775807, strict=True)
    sources_built: bool = Field(strict=True)
    window_id: UUID
    window_saved: bool = Field(strict=True)
    quota_usage_available: bool = Field(default=False, strict=True)
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def generation_is_not_usage_authority(self) -> "CoverageBuildProgress":
        if (
            self.processed_revision > self.target_revision
            or self.sources_built != (self.processed_revision == self.target_revision)
            or (self.window_saved and not self.sources_built)
            or self.quota_usage_available
        ):
            raise ValueError("coverage build progress cannot claim usage availability")
        return self
