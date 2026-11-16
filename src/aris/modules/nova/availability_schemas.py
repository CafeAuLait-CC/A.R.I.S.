import hashlib
import json
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from aris.core.contracts import Contract
from aris.modules.nova.schemas import ReservationWindow
from aris.modules.vega.target_schemas import GpuTargetFact

CONFLICT_PREVIEW_LIMIT = 20


class AvailabilityContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AvailabilityWindow(ReservationWindow):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AvailabilityQuery(AvailabilityWindow):
    gpu_uuid: str = Field(min_length=1, max_length=128)


class BasicReservationRules(AvailabilityContract):
    # All values require explicit configuration; there is no production default.
    max_duration_seconds: int = Field(gt=0, le=366 * 86400, strict=True)
    min_notice_seconds: int = Field(ge=0, le=366 * 86400, strict=True)
    max_advance_seconds: int = Field(ge=0, le=366 * 86400, strict=True)

    @model_validator(mode="after")
    def possible_notice(self) -> "BasicReservationRules":
        if self.max_advance_seconds < self.min_notice_seconds:
            raise ValueError("advance horizon must include minimum notice")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class MaintenanceCoverage(AvailabilityContract):
    gpu_uuid: str = Field(min_length=1, max_length=128)
    coverage: AvailabilityWindow
    windows: tuple[AvailabilityWindow, ...] = Field(max_length=1024)

    @model_validator(mode="after")
    def intervals_within_coverage(self) -> "MaintenanceCoverage":
        for window in self.windows:
            if window.start_at < self.coverage.start_at or window.end_at > self.coverage.end_at:
                raise ValueError("maintenance window lies outside known coverage")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ConflictSummary(AvailabilityContract):
    total: int = Field(ge=0)
    windows: tuple[AvailabilityWindow, ...] = Field(max_length=CONFLICT_PREVIEW_LIMIT)
    truncated: bool

    @model_validator(mode="after")
    def consistent_preview(self) -> "ConflictSummary":
        if (
            self.total < len(self.windows)
            or bool(self.total) != bool(self.windows)
            or self.truncated != (self.total > len(self.windows))
        ):
            raise ValueError("conflict preview and total differ")
        return self


AvailabilityCode = Literal[
    "AVAILABLE", "CONFLICT", "STALE_DATA", "POLICY_REJECTED", "NOT_EVALUATED", "NOT_FOUND"
]


class AvailabilityResult(AvailabilityContract):
    assessment_scope: Literal["resource_window"] = "resource_window"
    generated_at: AwareDatetime
    query: AvailabilityQuery
    code: AvailabilityCode
    reason: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z_]+$")
    available: bool | None
    resource: GpuTargetFact | None
    rules_snapshot: BasicReservationRules | None
    rules_fingerprint: str | None
    reservation_conflicts: ConflictSummary | None = None
    maintenance_conflicts: ConflictSummary | None = None

    @model_validator(mode="after")
    def honest_decision(self) -> "AvailabilityResult":
        expected = (
            True
            if self.code == "AVAILABLE"
            else None
            if self.code in {"NOT_EVALUATED", "STALE_DATA"}
            else False
        )
        if self.available is not expected:
            raise ValueError("availability does not match decision code")
        if self.code == "AVAILABLE" and (
            self.resource is None
            or not self.resource.present
            or self.resource.node_status != "ONLINE"
            or self.resource.telemetry_status != "CURRENT"
            or self.rules_snapshot is None
            or self.reservation_conflicts is None
            or self.maintenance_conflicts is None
            or self.reservation_conflicts.total
            or self.maintenance_conflicts.total
        ):
            raise ValueError("availability requires all evaluated checks")
        fingerprint = self.rules_snapshot.fingerprint if self.rules_snapshot else None
        if self.rules_fingerprint != fingerprint:
            raise ValueError("rules fingerprint does not match its snapshot")
        return self
