"""A draft policy applied to fixed historical usage, never a booking authorization."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.quota_evidence_schemas import QuotaEvidenceSelection
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_schemas import PolicyContract, PolicyName, QuotaLimit
from aris.modules.nova.policy_simulation import ExactGpuSeconds
from aris.modules.nova.policy_units import PolicyTimeWindow


class HistoricalQuotaQuery(PolicyContract):
    policy: PolicySourceReference
    expected_policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    usage: QuotaEvidenceSelection
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    period_at: AwareDatetime
    assumed_roles: tuple[PolicyName, ...] = Field(default=(), max_length=32)

    @field_validator("period_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_roles(self) -> "HistoricalQuotaQuery":
        if len(set(self.assumed_roles)) != len(self.assumed_roles):
            raise ValueError("historical quota roles must be explicit unique assumptions")
        return self


class HistoricalQuotaAssessment(PolicyContract):
    assessment_scope: Literal["draft_policy_historical_quota"] = "draft_policy_historical_quota"
    historical_only: Literal[True] = True
    booking_authorization: Literal[False] = False
    policy_activated: Literal[False] = False
    reservation_precharge: Literal[False] = False
    query: HistoricalQuotaQuery
    node_id: str
    owner_id: UUID
    period: PolicyTimeWindow
    sampled_until: AwareDatetime
    captured_at: AwareDatetime
    snapshot_created_at: AwareDatetime
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["within_limit", "exhausted", "unlimited", "not_evaluated"]
    reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    selected_assumed_role: str | None = None
    limit: QuotaLimit | None = None
    used_gpu_seconds: ExactGpuSeconds | None = None
    remaining_gpu_seconds: ExactGpuSeconds | None = None

    @model_validator(mode="after")
    def honest(self) -> "HistoricalQuotaAssessment":
        if (
            (self.node_id, self.owner_id) != (self.query.usage.node_id, self.query.usage.owner_id)
            or not self.sampled_until <= self.captured_at <= self.snapshot_created_at
            or (
                self.state == "not_evaluated"
                and (self.used_gpu_seconds is not None or self.remaining_gpu_seconds is not None)
            )
            or (
                self.state in {"within_limit", "exhausted"}
                and (
                    self.limit is None
                    or self.limit.kind != "limited"
                    or self.used_gpu_seconds is None
                    or self.remaining_gpu_seconds is None
                )
            )
            or (
                self.state == "unlimited"
                and (
                    self.limit is None
                    or self.limit.kind != "unlimited"
                    or self.remaining_gpu_seconds is not None
                )
            )
        ):
            raise ValueError("historical quota result has inconsistent scope or numeric authority")
        return self
