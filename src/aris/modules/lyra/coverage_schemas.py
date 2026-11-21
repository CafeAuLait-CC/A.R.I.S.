"""Local processing coverage is a prerequisite, not a complete quota snapshot."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.history_schemas import HistoricalNodeUserResolution
from aris.modules.lyra.ledger_schemas import UsageLedgerHead, UsagePostingConfiguration
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.usage_intervals import IntervalContract, ObservationReference

MAX_COVERAGE_BYTES = 8 * 1024 * 1024
MAX_COVERAGE_CELLS = 32768
MICROSECOND = timedelta(microseconds=1)


def coverage_identity_key(run: ResidencyRun) -> tuple[str, str, int, str]:
    """A growing run can have multiple immutable, separately assessed parent prefixes."""
    return run.id, run.rule_fingerprint, run.intervals, run.evidence_fingerprint


class ProcessingCoverageConfiguration(IntervalContract):
    method: Literal["strict_sampled_residency_edges"] = "strict_sampled_residency_edges"
    trace: UsageTraceConfiguration
    recording: UsagePostingConfiguration
    gpu_uuids: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("gpu_uuids")
    @classmethod
    def complete_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not 1 <= len(item) <= 128 for item in value):
            raise ValueError("coverage requires explicit unique GPU targets")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def one_method(self) -> "ProcessingCoverageConfiguration":
        if self.recording.timing_fingerprint != residency_fingerprint(
            self.trace.continuity, self.trace.formation
        ):
            raise ValueError("coverage timing differs from its source configuration")
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("processing_coverage_configuration", canonical_trace(self))


class ProcessingCoverageRequest(IntervalContract):
    id: UUID
    attestation_id: UUID
    expected_attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    ledger: UsageLedgerHead

    @field_validator("start_at", "end_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_window(self) -> "ProcessingCoverageRequest":
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=366):
            raise ValueError("coverage requires a bounded positive UTC window")
        return self


class CoverageHazard(IntervalContract):
    start_at: AwareDatetime
    end_at: AwareDatetime
    reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)

    @model_validator(mode="after")
    def positive(self) -> "CoverageHazard":
        if self.start_at >= self.end_at:
            raise ValueError("coverage hazard requires positive time")
        return self


class CoverageIdentity(IntervalContract):
    run: ResidencyRun = Field(repr=False)
    identity: HistoricalNodeUserResolution = Field(repr=False)

    @model_validator(mode="after")
    def whole_parent(self) -> "CoverageIdentity":
        query = self.identity.query
        if (query.node_id, query.os_username, query.start_at, query.end_at) != (
            self.run.first.node_id,
            self.run.username,
            self.run.first.compute_observed_at,
            self.run.last.compute_observed_at,
        ):
            raise ValueError("coverage identity must preserve its whole parent run")
        return self


class CoverageRecordingReference(IntervalContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    proof_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    entry_id: UUID
    entry_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProcessingCoverageCell(IntervalContract):
    gpu_uuid: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    state: Literal["observed_idle", "recorded", "unknown", "uncovered"]
    reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    previous: int | None = Field(default=None, ge=0, le=128)
    current: int | None = Field(default=None, ge=0, le=128)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    identity_index: int | None = Field(default=None, ge=0, lt=128)
    recording_index: int | None = Field(default=None, ge=0, lt=128)
    owner_id: UUID | None = None

    @property
    def microseconds(self) -> int:
        return (self.end_at - self.start_at) // MICROSECOND

    @model_validator(mode="after")
    def honest_state(self) -> "ProcessingCoverageCell":
        recorded = self.state == "recorded"
        if (
            self.start_at >= self.end_at
            or (self.previous is None) != (self.current is None)
            or (self.previous is not None and self.previous == self.current)
            or recorded != (self.owner_id is not None)
            or recorded != (self.recording_index is not None)
            or (recorded and (self.identity_index is None or self.run_id is None))
            or (self.state in {"recorded", "observed_idle"} and self.previous is None)
            or (
                self.state == "recorded"
                and self.reason not in {"COMMITTED_CLOSED_RECORDING", "COMMITTED_STABLE_RECORDING"}
            )
            or (self.state == "observed_idle" and self.reason != "NO_COMPUTE_APPS_AT_ENDPOINTS")
        ):
            raise ValueError("coverage cell cannot claim stronger recording authority")
        return self


class ProcessingCoverageAnalysis(IntervalContract):
    assessment_scope: Literal["attested_source_processing_coverage"] = (
        "attested_source_processing_coverage"
    )
    quota_usage_available: Literal[False] = False
    total_usage_microseconds: None = None
    full_source_prefix_assessed: Literal[False] = False
    request: ProcessingCoverageRequest
    configuration: ProcessingCoverageConfiguration
    attestation: StoredTraceAttestation
    observations: tuple[ObservationReference, ...] = Field(max_length=129)
    hazards: tuple[CoverageHazard, ...] = Field(max_length=128)
    invalid_observation_times: tuple[AwareDatetime, ...] = Field(max_length=129)
    identities: tuple[CoverageIdentity, ...] = Field(max_length=128, repr=False)
    recordings: tuple[CoverageRecordingReference, ...] = Field(max_length=128)
    cells: tuple[ProcessingCoverageCell, ...] = Field(max_length=MAX_COVERAGE_CELLS)
    identity_revision: int | None = Field(default=None, ge=1)
    known_recorded_microseconds: int = Field(ge=0)
    unknown_gpu_microseconds: int = Field(ge=0)
    uncovered_gpu_microseconds: int = Field(ge=0)
    complete_within_source_window: bool = Field(strict=True)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_local_partition(self) -> "ProcessingCoverageAnalysis":
        request, config = self.request, self.configuration
        if (
            request.expected_configuration_fingerprint != config.fingerprint
            or request.ledger.node_id != config.trace.node_id
            or request.attestation_id != self.attestation.request.id
            or request.expected_attestation_fingerprint != self.attestation.fingerprint
            or self.attestation.configuration_fingerprint != config.trace.fingerprint
            or self.read_at < max(request.end_at, self.attestation.created_at)
            or any(
                (value.agent_id, value.node_id) != (config.trace.agent_id, config.trace.node_id)
                for value in self.observations
            )
            or len({coverage_identity_key(value.run) for value in self.identities})
            != len(self.identities)
        ):
            raise ValueError("coverage configuration or original source binding differs")
        position = {gpu: request.start_at for gpu in config.gpu_uuids}
        if (
            tuple(sorted(self.cells, key=lambda value: (value.gpu_uuid, value.start_at)))
            != self.cells
        ):
            raise ValueError("coverage cells require canonical GPU/time ordering")
        for cell in self.cells:
            if (
                cell.gpu_uuid not in position
                or cell.start_at != position[cell.gpu_uuid]
                or cell.end_at > request.end_at
            ):
                raise ValueError("each GPU requires an exact nonoverlapping window partition")
            position[cell.gpu_uuid] = cell.end_at
            if cell.previous is not None and cell.current is not None:
                if max(cell.previous, cell.current) >= len(self.observations):
                    raise ValueError("coverage observation reference is absent")
                previous, current = (
                    self.observations[cell.previous],
                    self.observations[cell.current],
                )
                if (
                    not previous.compute_observed_at
                    <= cell.start_at
                    < cell.end_at
                    <= current.compute_observed_at
                ):
                    raise ValueError("coverage cell exceeds its original positive sample edge")
            if cell.identity_index is not None:
                if (
                    cell.identity_index >= len(self.identities)
                    or self.identities[cell.identity_index].run.id != cell.run_id
                ):
                    raise ValueError("coverage identity binding differs")
            if cell.state == "recorded":
                assert cell.identity_index is not None and cell.recording_index is not None
                if (
                    cell.recording_index >= len(self.recordings)
                    or self.recordings[cell.recording_index].run_id != cell.run_id
                    or self.identities[cell.identity_index].identity.user_id != cell.owner_id
                    or not self.identities[cell.identity_index].run.first.compute_observed_at
                    <= cell.start_at
                    < cell.end_at
                    <= self.identities[cell.identity_index].run.last.compute_observed_at
                ):
                    raise ValueError("recorded coverage lacks its committed identity or recording")
        if (
            any(value != request.end_at for value in position.values())
            or any(
                value.identity.directory_revision != self.identity_revision
                or value.identity.query.as_of != self.read_at
                for value in self.identities
            )
            or self.known_recorded_microseconds
            != sum(value.microseconds for value in self.cells if value.state == "recorded")
            or self.unknown_gpu_microseconds
            != sum(value.microseconds for value in self.cells if value.state == "unknown")
            or self.uncovered_gpu_microseconds
            != sum(value.microseconds for value in self.cells if value.state == "uncovered")
            or self.complete_within_source_window
            != (self.unknown_gpu_microseconds == self.uncovered_gpu_microseconds == 0)
            or self.fingerprint != processing_coverage_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_COVERAGE_BYTES
        ):
            raise ValueError("coverage totals or captured evidence differ from their local scope")
        return self


def processing_coverage_fingerprint(value: ProcessingCoverageAnalysis) -> str:
    return trace_fingerprint("processing_coverage", canonical_trace(value, exclude={"fingerprint"}))


class StoredProcessingCoverage(IntervalContract):
    analysis: ProcessingCoverageAnalysis
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bound(self) -> "StoredProcessingCoverage":
        if (
            self.created_at < self.analysis.read_at
            or self.fingerprint != coverage_manifest_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_COVERAGE_BYTES
        ):
            raise ValueError("coverage manifest differs from its captured analysis")
        return self


class ProcessingCoverageReceipt(IntervalContract):
    manifest: StoredProcessingCoverage
    duplicate: bool = Field(strict=True)


def coverage_manifest_fingerprint(value: StoredProcessingCoverage) -> str:
    return trace_fingerprint(
        "processing_coverage_manifest", canonical_trace(value, exclude={"fingerprint"})
    )
