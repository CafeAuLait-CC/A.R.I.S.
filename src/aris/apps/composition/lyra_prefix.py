"""Finite source exclusion maintenance, with the eligible boundary kept separate."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, JsonValue, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.modules.lyra.source_prefix_schemas import AdvanceSourcePrefix, SourcePrefixStatus
from aris.modules.lyra.source_prefix_service import SourcePrefixService
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from aris.modules.vega.usage_service import VegaUsageObservations

_NAMESPACE = UUID("5924eab6-7b03-5bd8-8407-2bfe33c5ec75")


class LyraPrefixRuntimeConfig(IntervalContract):
    enabled: bool = Field(default=False, strict=True)
    configuration: UsageTraceConfiguration | None = None
    before: AwareDatetime | None = None
    max_proofs_per_tick: int = Field(default=1, ge=1, le=8, strict=True)
    poll_seconds: float = Field(default=1, ge=0.1, le=60)

    @model_validator(mode="after")
    def explicit_boundary(self) -> "LyraPrefixRuntimeConfig":
        if self.enabled and (self.configuration is None or self.before is None):
            raise ValueError("enabled prefix maintenance requires explicit source and boundary")
        return self

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("private_lyra_prefix_runtime", canonical_trace(self))


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate prefix configuration key")
        result[key] = value
    return result


def load_lyra_prefix_config(path: Path) -> LyraPrefixRuntimeConfig:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise ValueError("prefix configuration requires a private regular file")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("prefix configuration exceeds its byte budget")
    return LyraPrefixRuntimeConfig.model_validate(json.loads(raw, object_pairs_hook=_unique_keys))


@dataclass(frozen=True)
class LyraPrefixComponents:
    service: SourcePrefixService | None
    configuration: LyraPrefixRuntimeConfig
    clock: Callable[[], datetime]

    def _summary(
        self, state: str, status: SourcePrefixStatus | None = None
    ) -> dict[str, JsonValue]:
        return {
            "mode": "lyra-source-prefix",
            "state": state,
            "paused": not self.configuration.enabled,
            "configuration_fingerprint": self.configuration.fingerprint,
            "source_deletion_enabled": False,
            "quota_usage_available": False,
            "status": status.model_dump(mode="json") if status else None,
            "generated_at": self.clock().isoformat(),
        }

    def run_once(self, *, status_only: bool = False) -> dict[str, JsonValue]:
        config = self.configuration
        if self.service is None or config.configuration is None or config.before is None:
            return self._summary("paused")
        status = self.service.status(config.configuration, config.before)
        latest = status.latest
        if (
            latest is not None
            and latest.max_observed_at is not None
            and latest.max_observed_at >= config.before
        ):
            return self._summary("boundary_reached", status)
        if (latest.through_revision if latest else 0) == status.source_revision:
            return self._summary("caught_up", status)
        if status_only or not config.enabled:
            return self._summary("pending" if status_only else "paused", status)
        key = (
            config.configuration.fingerprint
            + ":"
            + (latest.fingerprint if latest else "origin")
            + ":"
            + str(config.max_proofs_per_tick)
        )
        self.service.advance(
            UsageTraceConsumeContext(request_id="prefix-worker-step"),
            AdvanceSourcePrefix(
                id=uuid5(_NAMESPACE, key),
                expected_configuration_fingerprint=config.configuration.fingerprint,
                previous=latest,
                limit=config.max_proofs_per_tick,
            ),
        )
        return self._summary("advanced", self.service.status(config.configuration, config.before))


def build_lyra_prefix_components(
    sessions: sessionmaker[Session],
    config: LyraPrefixRuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
    status_only: bool = False,
) -> LyraPrefixComponents:
    config = LyraPrefixRuntimeConfig.model_validate(config.model_dump(mode="json"))
    now = clock or (lambda: datetime.now(UTC))
    if (
        (not config.enabled and not status_only)
        or config.configuration is None
        or config.before is None
    ):
        return LyraPrefixComponents(None, config, now)
    archive = VegaUsageArchive(sessions, clock=now)
    observations = VegaUsageObservations(sessions)
    trace = UsageTraceConsumerService(
        sessions, archive, observations, configuration=config.configuration, clock=now
    )
    proofs = TraceAttestationService(sessions, trace, clock=now)
    service = SourcePrefixService(
        sessions, proofs, configuration=config.configuration if config.enabled else None, clock=now
    )
    return LyraPrefixComponents(service, config, now)
