"""One explicitly selected historical coverage build, separate from recording and scheduling."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from uuid import UUID

from pydantic import Field, JsonValue, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_build_schemas import (
    AdmitCoverageWindow,
    AdvanceCoverageBuild,
    CoverageBuildProgress,
)
from aris.modules.lyra.coverage_build_service import CoverageBuildService
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.source_prefix_schemas import SourcePrefixSelection
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConsumeContext,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from aris.modules.vega.usage_service import VegaUsageObservations

MAX_CONFIG_BYTES = 65536


class CoverageBuildTarget(SourcePrefixSelection):
    id: UUID
    query: UsageWindowQuery


class LyraCoverageRuntimeConfig(IntervalContract):
    enabled: bool = Field(default=False, strict=True)
    configuration: ProcessingCoverageConfiguration | None = None
    target: CoverageBuildTarget | None = None
    poll_seconds: float = Field(default=1, ge=0.1, le=60)
    transaction_timeout_ms: int = Field(default=30000, ge=1000, le=300000, strict=True)

    @model_validator(mode="after")
    def explicit_target(self) -> "LyraCoverageRuntimeConfig":
        if self.enabled and (self.configuration is None or self.target is None):
            raise ValueError(
                "enabled coverage worker requires an explicit configuration and target"
            )
        if (
            self.configuration is not None
            and self.target is not None
            and (self.target.query.node_id != self.configuration.trace.node_id)
        ):
            raise ValueError("coverage worker target differs from its configured node")
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("private_lyra_coverage_runtime", canonical_trace(self))


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate coverage configuration key")
        result[key] = value
    return result


def load_lyra_coverage_config(path: Path) -> LyraCoverageRuntimeConfig:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_CONFIG_BYTES
        ):
            raise ValueError("coverage configuration requires a private regular file")
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("coverage configuration exceeds its read budget")
    return LyraCoverageRuntimeConfig.model_validate(json.loads(raw, object_pairs_hook=_unique_keys))


@dataclass(frozen=True)
class LyraCoverageComponents:
    service: CoverageBuildService | None
    configuration: LyraCoverageRuntimeConfig
    clock: Callable[[], datetime]

    def _summary(
        self, state: str, progress: CoverageBuildProgress | None = None
    ) -> dict[str, JsonValue]:
        report: dict[str, JsonValue] = {
            "mode": "lyra-coverage-build",
            "state": state,
            "configuration_fingerprint": self.configuration.fingerprint,
            "paused": not self.configuration.enabled,
            "quota_usage_available": False,
            "generated_at": self.clock().isoformat(),
            "plan_id": str(self.configuration.target.id) if self.configuration.target else None,
            "progress": progress.model_dump(mode="json") if progress else None,
            "snapshot_id": None,
            "coverage": "not_assessed",
        }
        if progress is not None and progress.window_saved and self.service is not None:
            window = self.service.windows.exact(progress.window_id).window
            report.update(snapshot_id=str(progress.window_id), coverage=window.coverage)
        return report

    def run_once(self, *, status_only: bool = False) -> dict[str, JsonValue]:
        config = self.configuration
        target = config.target
        if self.service is None or target is None or config.configuration is None:
            return self._summary("paused" if not config.enabled else "not_configured")
        try:
            plan = self.service.exact(target.id)
        except DomainError as error:
            if error.code != "NOT_FOUND":
                raise
            if status_only:
                return self._summary("not_started")
            if not config.enabled:
                return self._summary("paused")
            plan = self.service.admit_current(
                UsageTraceConsumeContext(request_id="coverage-worker-admission"),
                AdmitCoverageWindow(
                    prefix_checkpoint=target.prefix_checkpoint,
                    id=target.id,
                    query=target.query,
                    expected_configuration_fingerprint=config.configuration.fingerprint,
                ),
            )
            return self._summary("admitted", self.service.progress(plan.request.id))
        if (
            plan.request.query != target.query
            or plan.configuration != config.configuration
            or plan.request.prefix_checkpoint != target.prefix_checkpoint
        ):
            raise DomainError("CONFLICT", "Coverage worker target has different fixed input", 409)
        progress = self.service.progress(target.id)
        if status_only:
            return self._summary("finished" if progress.window_saved else "pending", progress)
        if not config.enabled:
            return self._summary("paused", progress)
        if progress.window_saved:
            return self._summary("finished", progress)
        context = UsageTraceConsumeContext(request_id="coverage-worker-step")
        if not progress.sources_built:
            self.service.advance(
                context,
                AdvanceCoverageBuild(
                    id=target.id,
                    expected_plan_fingerprint=plan.fingerprint,
                    expected_revision=progress.processed_revision,
                ),
            )
            return self._summary("source_built", self.service.progress(target.id))
        self.service.finish(context, target.id)
        return self._summary("finished", self.service.progress(target.id))


def build_lyra_coverage_components(
    sessions: sessionmaker[Session],
    config: LyraCoverageRuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
    status_only: bool = False,
) -> LyraCoverageComponents:
    config = LyraCoverageRuntimeConfig.model_validate(config.model_dump(mode="json"))
    now = clock or (lambda: datetime.now(UTC))
    if (
        (not config.enabled and not status_only)
        or config.configuration is None
        or config.target is None
    ):
        return LyraCoverageComponents(None, config, now)
    domain = config.configuration
    archive = VegaUsageArchive(sessions, clock=now)
    observations = VegaUsageObservations(sessions)
    identities = NodeUserHistoryService(sessions, clock=now)
    trace = UsageTraceConsumerService(
        sessions, archive, observations, configuration=domain.trace, clock=now
    )
    attestations = TraceAttestationService(sessions, trace, clock=now)
    coverage = ProcessingCoverageService(
        sessions,
        attestations,
        archive,
        observations,
        identities,
        configuration=domain if config.enabled else None,
        clock=now,
    )
    windows = UsageWindowSnapshotService(
        sessions,
        coverage,
        archive,
        identities,
        configuration=domain if config.enabled else None,
        clock=now,
        timeout_ms=config.transaction_timeout_ms,
    )
    service = CoverageBuildService(
        coverage, windows, clock=now, timeout_ms=config.transaction_timeout_ms
    )
    return LyraCoverageComponents(service, config, now)
