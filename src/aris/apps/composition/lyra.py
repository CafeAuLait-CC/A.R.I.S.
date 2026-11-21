"""Private opt-in assembly; domain services retain all evidence and write authority."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import Literal

from pydantic import Field, JsonValue, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_service import NodeUserHistoryService
from aris.modules.lyra.derivation_service import UsageDerivationService
from aris.modules.lyra.ledger_index_maintenance import LedgerIndexMaintenance
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration, UsagePostingContext
from aris.modules.lyra.ledger_service import UsageLedgerService
from aris.modules.lyra.partition_recording_service import PartitionRecordingService
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.recording_pipeline_schemas import (
    RecordingPipelineConfiguration,
    RecordingPipelineLimits,
)
from aris.modules.lyra.recording_pipeline_service import RecordingPipelineService
from aris.modules.lyra.recording_plan_service import RecordingPlanService
from aris.modules.lyra.recording_pump import RecordingPump
from aris.modules.lyra.recording_pump_schemas import RecordingPumpConfiguration, RecordingPumpTick
from aris.modules.lyra.reservation_links import ResidencyReservationService
from aris.modules.lyra.residency_attribution import ResidencyAttributionService
from aris.modules.lyra.residency_evidence_service import ResidencyRunEvidenceService
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.residency_layout_verification_service import StableResidencyWindowService
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
from aris.modules.lyra.residency_segment_service import ResidencySegmentService
from aris.modules.lyra.residency_sessions import ResidencyFormationRule, residency_fingerprint
from aris.modules.lyra.run_recording_service import ClosedRunRecordingService
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.lyra.source_batching import SourceBatchSelection
from aris.modules.lyra.stable_dispatch_schemas import StableAutoDispatchReceipt
from aris.modules.lyra.stable_pipeline_schemas import StableRecordingPipelineConfiguration
from aris.modules.lyra.stable_pipeline_service import StableRecordingPipelineService
from aris.modules.lyra.stable_pump import StableRecordingPump
from aris.modules.lyra.stable_pump_schemas import (
    StableRecordingPumpConfiguration,
    StableRecordingPumpTick,
)
from aris.modules.lyra.stable_window_acceptance_service import StableWindowAcceptanceService
from aris.modules.lyra.stable_window_plan_service import StableWindowPlanService
from aris.modules.lyra.stable_window_recording_service import StableWindowRecordingService
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.nova.usage_link_service import ReservationUsageLinkService
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from aris.modules.vega.usage_service import VegaUsageObservations

MAX_LYRA_RUNTIME_BYTES = 65536


class LyraSourceBinding(IntervalContract):
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    initial_after_seq: int = Field(ge=0, lt=9223372036854775807, strict=True)


class LyraRuntimeConfig(SourceBatchSelection):
    enabled: bool = Field(default=False, strict=True)
    source_ref: str = Field(default="explicit-private-lyra-runtime", min_length=1, max_length=128)
    recording: UsagePostingConfiguration | None = None
    continuity: UsageContinuityRule | None = None
    formation: ResidencyFormationRule | None = None
    method: ResidencyPartitionMethod | StableResidencyLayoutMethod | None = None
    identity_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)
    sources: tuple[LyraSourceBinding, ...] = Field(default=(), max_length=16)
    jobs: RecordingJobLimits | None = None
    pipeline_limits: RecordingPipelineLimits = Field(default_factory=RecordingPipelineLimits)
    source_limit: int = Field(default=32, ge=1, le=128, strict=True)
    dispatch_limit: int = Field(default=1, ge=1, le=8, strict=True)
    poll_seconds: int = Field(default=5, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def explicit_recording_context(self) -> "LyraRuntimeConfig":
        if self.source_batching is not None and (
            (self.method is not None and not isinstance(self.method, StableResidencyLayoutMethod))
            or self.source_batching.minimum_messages > self.source_limit
        ):
            raise ValueError("source batching requires stable mode and a sufficient page limit")
        required = (
            self.recording,
            self.continuity,
            self.formation,
            self.method,
            self.identity_revision,
            self.jobs,
        )
        if self.enabled and (any(value is None for value in required) or not self.sources):
            raise ValueError(
                "enabled Lyra worker requires explicit timing, identity, sources and job limits"
            )
        if (
            self.recording is not None
            and self.continuity is not None
            and self.formation is not None
            and self.recording.timing_fingerprint
            != residency_fingerprint(self.continuity, self.formation)
        ):
            raise ValueError("Lyra recording timing differs from the configured source method")
        if (
            self.method is not None
            and self.formation is not None
            and self.method.max_intervals < self.formation.minimum_intervals
        ):
            raise ValueError("Lyra partitions cannot satisfy the configured formation rule")
        for values in (
            [value.consumer_id for value in self.sources],
            [value.agent_id for value in self.sources],
            [value.node_id for value in self.sources],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Lyra source bindings must be unique")
        if (
            isinstance(self.method, StableResidencyLayoutMethod)
            and self.formation is not None
            and self.method.max_intervals < 2 * self.formation.minimum_intervals - 1
        ):
            raise ValueError(
                "Stable Lyra capacity cannot satisfy the balanced final-tail formation rule"
            )
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("private_lyra_runtime", canonical_trace(self))


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Lyra configuration key")
        result[key] = value
    return result


def load_lyra_config(path: Path) -> LyraRuntimeConfig:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_LYRA_RUNTIME_BYTES
        ):
            raise ValueError("Lyra configuration requires a private regular file of at most 64 KiB")
        raw = stream.read(MAX_LYRA_RUNTIME_BYTES + 1)
    if len(raw) > MAX_LYRA_RUNTIME_BYTES:
        raise ValueError("Lyra configuration exceeds its read budget")
    return LyraRuntimeConfig.model_validate(json.loads(raw, object_pairs_hook=_unique_keys))


def _stable_dispatch_summary(
    value: StableAutoDispatchReceipt | None,
) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    dispatched, attempt = value.dispatch, value.attempt
    return {
        "state": value.state,
        "layout_key": dispatched.plan.key if dispatched else attempt.key if attempt else None,
        "version": dispatched.plan.version if dispatched else None,
        "next_window": dispatched.plan.next_window if dispatched else None,
        "planned_windows": dispatched.plan.page.total_planned_windows if dispatched else None,
        "enqueued": len(dispatched.job_ids) if dispatched else 0,
        "failures": attempt.failures if attempt else 0,
        "code": attempt.last_error_code if attempt else None,
        "available_at": attempt.available_at.isoformat() if attempt else None,
    }


def summarize_recording_tick(
    value: RecordingPumpTick | StableRecordingPumpTick, fingerprint: str
) -> dict[str, JsonValue]:
    if isinstance(value, StableRecordingPumpTick):
        dispatch_summary = _stable_dispatch_summary(value.dispatch)
    else:
        dispatched = value.dispatch
        dispatch_summary = (
            {
                "plan_id": str(dispatched.progress.plan.request.id),
                "version": dispatched.progress.version,
                "next_partition": dispatched.progress.next_partition,
                "partition_count": dispatched.progress.plan.partition_count,
                "all_enqueued": dispatched.progress.all_enqueued,
                "enqueued": len(dispatched.job_ids),
            }
            if dispatched is not None
            else None
        )
    summary: dict[str, JsonValue] = {
        "mode": "lyra-recording-stable"
        if isinstance(value, StableRecordingPumpTick)
        else "lyra-recording",
        "paused": value.paused,
        "configuration_fingerprint": fingerprint,
        "quota_usage_available": False,
        "time_window_coverage": "not_assessed",
        "generated_at": value.generated_at.isoformat(),
        "source": value.source.model_dump(mode="json") if value.source is not None else None,
        "dispatch": dispatch_summary,
        "claimed_job_id": str(value.claimed_job_id) if value.claimed_job_id is not None else None,
        "job": value.job.model_dump(mode="json") if value.job is not None else None,
        "failures": [failure.model_dump(mode="json") for failure in value.failures],
    }
    if isinstance(value, StableRecordingPumpTick):
        summary["ledger_index"] = (
            value.ledger_index.model_dump(mode="json") if value.ledger_index is not None else None
        )
    return summary


@dataclass(frozen=True)
class LyraComponents:
    pump: RecordingPump | StableRecordingPump | None
    configuration_fingerprint: str
    clock: Callable[[], datetime]
    processing_mode: Literal["closed", "stable"] = "closed"

    def run_once(self) -> dict[str, JsonValue]:
        if self.pump is None:
            return summarize_recording_tick(
                StableRecordingPumpTick(paused=True, generated_at=self.clock())
                if self.processing_mode == "stable"
                else RecordingPumpTick(paused=True, generated_at=self.clock()),
                self.configuration_fingerprint,
            )
        return summarize_recording_tick(
            self.pump.tick(UsagePostingContext(request_id="lyra-recording-worker")),
            self.configuration_fingerprint,
        )


def build_lyra_components(
    sessions: sessionmaker[Session],
    config: LyraRuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
) -> LyraComponents:
    config = LyraRuntimeConfig.model_validate(config.model_dump(mode="json"))
    now = clock or (lambda: datetime.now(UTC))
    if not config.enabled:
        return LyraComponents(
            None,
            config.fingerprint,
            now,
            "stable" if isinstance(config.method, StableResidencyLayoutMethod) else "closed",
        )
    if (
        config.recording is None
        or config.continuity is None
        or config.formation is None
        or config.method is None
        or config.identity_revision is None
        or config.jobs is None
    ):
        raise ValueError("enabled Lyra worker requires a complete recording context")
    archive = VegaUsageArchive(sessions, clock=now)
    observations = VegaUsageObservations(sessions)
    identities = NodeUserHistoryService(sessions, clock=now)
    derivations = UsageDerivationService(
        sessions,
        observations,
        ResidencyAttributionService(sessions, identities),
        ResidencyReservationService(sessions, ReservationUsageLinkService(sessions, clock=now)),
        clock=now,
    )
    ledger = UsageLedgerService(sessions, derivations, configuration=config.recording, clock=now)
    verifier = UsageTraceConsumerService(sessions, archive, observations, clock=now)
    attestations = TraceAttestationService(sessions, verifier, clock=now)
    segments = ResidencySegmentStore(
        sessions,
        ResidencySegmentService(sessions, attestations, archive, observations, clock=now),
        clock=now,
    )
    evidence = ResidencyPartitionService(
        sessions,
        attestations,
        verifier,
        segments,
        archive,
        method=ResidencyPartitionMethod(max_intervals=config.method.max_intervals)
        if isinstance(config.method, StableResidencyLayoutMethod)
        else config.method,
        clock=now,
    )
    whole = ClosedRunRecordingService(
        sessions,
        ResidencyRunEvidenceService(sessions, archive, clock=now),
        archive,
        derivations,
        ledger,
        clock=now,
        timeout_ms=config.jobs.transaction_timeout_ms,
    )
    if isinstance(config.method, StableResidencyLayoutMethod):
        stable_evidence = StableResidencyWindowService(
            sessions,
            evidence,
            method=config.method,
            clock=now,
            timeout_ms=min(config.pipeline_limits.timeout_ms, 10000),
        )
        accepts = StableWindowAcceptanceService(
            sessions,
            stable_evidence,
            identities,
            configuration=config.recording,
            clock=now,
            timeout_ms=config.jobs.transaction_timeout_ms,
        )
        stable_writer = StableWindowRecordingService(
            sessions,
            accepts,
            derivations,
            ledger,
            timeout_ms=config.jobs.transaction_timeout_ms,
        )
        stable_jobs = RecordingJobService(
            sessions,
            whole,
            stable_recorder=stable_writer,
            limits=config.jobs,
            clock=now,
        )
        stable_plans = StableWindowPlanService(
            sessions,
            stable_evidence,
            accepts,
            stable_jobs,
            configuration=config.recording,
            clock=now,
            timeout_ms=config.pipeline_limits.timeout_ms,
        )
        stable_pipelines = []
        for binding in config.sources:
            stable_trace_config = UsageTraceConfiguration(
                **binding.model_dump(),
                source_ref=config.source_ref,
                continuity=config.continuity,
                formation=config.formation,
            )
            stable_trace = UsageTraceConsumerService(
                sessions,
                archive,
                observations,
                configuration=stable_trace_config,
                clock=now,
            )
            stable_pipelines.append(
                StableRecordingPipelineService(
                    sessions,
                    stable_trace,
                    attestations,
                    segments,
                    stable_plans,
                    configuration=StableRecordingPipelineConfiguration(
                        source_ref=config.source_ref,
                        trace=stable_trace_config,
                        recording=config.recording,
                        method=config.method,
                        identity_revision=config.identity_revision,
                    ),
                    limits=config.pipeline_limits,
                    identity_head=identities,
                    clock=now,
                )
            )
        stable_pump = StableRecordingPump(
            tuple(stable_pipelines),
            archive,
            stable_plans,
            stable_jobs,
            clock=now,
            batch_reader=archive if config.source_batching is not None else None,
            ledger_index=LedgerIndexMaintenance(
                sessions, tuple(binding.node_id for binding in config.sources), clock=now
            ),
            configuration=StableRecordingPumpConfiguration(
                source_batching=config.source_batching,
                recording=config.recording,
                method=config.method,
                source_limit=config.source_limit,
                dispatch_limit=config.dispatch_limit,
            ),
        )
        return LyraComponents(stable_pump, config.fingerprint, now, "stable")
    writer = PartitionRecordingService(
        sessions,
        evidence,
        identities,
        derivations,
        ledger,
        clock=now,
        timeout_ms=config.jobs.transaction_timeout_ms,
    )
    jobs = RecordingJobService(
        sessions, whole, partition_recorder=writer, limits=config.jobs, clock=now
    )
    plans = RecordingPlanService(
        sessions, evidence, ledger, jobs, clock=now, timeout_ms=config.pipeline_limits.timeout_ms
    )
    pipelines = []
    for source in config.sources:
        trace_config = UsageTraceConfiguration(
            **source.model_dump(),
            source_ref=config.source_ref,
            continuity=config.continuity,
            formation=config.formation,
        )
        trace = UsageTraceConsumerService(
            sessions, archive, observations, configuration=trace_config, clock=now
        )
        pipeline_config = RecordingPipelineConfiguration(
            source_ref=config.source_ref,
            trace=trace_config,
            recording=config.recording,
            method=config.method,
            identity_revision=config.identity_revision,
        )
        pipelines.append(
            RecordingPipelineService(
                sessions,
                trace,
                attestations,
                segments,
                plans,
                configuration=pipeline_config,
                limits=config.pipeline_limits,
                clock=now,
            )
        )
    pump = RecordingPump(
        tuple(pipelines),
        archive,
        plans,
        jobs,
        clock=now,
        configuration=RecordingPumpConfiguration(
            recording=config.recording,
            method=config.method,
            source_limit=config.source_limit,
            dispatch_limit=config.dispatch_limit,
        ),
    )
    return LyraComponents(pump, config.fingerprint, now)
