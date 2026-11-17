"""Typed policy source only; validation never grants access or publishes a policy."""

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, field_validator, model_validator

from aris.core.contracts import Contract

YEAR_SECONDS = 366 * 86400
WEEK_SECONDS = 7 * 86400
PolicyName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
PositiveSeconds = Annotated[int, Field(gt=0, le=YEAR_SECONDS, strict=True)]
ConcurrentGpus = Annotated[int, Field(ge=1, le=1000, strict=True)]


class PolicyContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class LimitedQuota(PolicyContract):
    kind: Literal["limited"]
    gpu_seconds: int = Field(ge=0, le=1000 * WEEK_SECONDS, strict=True)


class UnlimitedQuota(PolicyContract):
    kind: Literal["unlimited"]


QuotaLimit = Annotated[LimitedQuota | UnlimitedQuota, Field(discriminator="kind")]


class RollingWeek(PolicyContract):
    kind: Literal["rolling_7d"]


class CalendarWeek(PolicyContract):
    kind: Literal["calendar_week"]
    week_starts_on: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]


QuotaWindow = Annotated[RollingWeek | CalendarWeek, Field(discriminator="kind")]


class GlobalQuotaRules(PolicyContract):
    scope: Literal["global"]
    basis: Literal["actual_gpu_usage"]
    window: QuotaWindow
    default_limit: QuotaLimit


class NodeQuotaLimit(PolicyContract):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    limit: QuotaLimit


class NodeQuotaRules(PolicyContract):
    scope: Literal["per_node"]
    basis: Literal["actual_gpu_usage"]
    window: QuotaWindow
    node_limits: tuple[NodeQuotaLimit, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def unique_nodes(self) -> "NodeQuotaRules":
        if len({item.node_id for item in self.node_limits}) != len(self.node_limits):
            raise ValueError("per-node quota limits must have unique targets")
        return self


QuotaRules = Annotated[GlobalQuotaRules | NodeQuotaRules, Field(discriminator="scope")]


class GpuTimeWeight(PolicyContract):
    numerator: int = Field(ge=1, le=1000, strict=True)
    denominator: int = Field(ge=1, le=1000, strict=True)


class GpuModelWeight(PolicyContract):
    model: str = Field(min_length=1, max_length=256)
    weight: GpuTimeWeight

    @field_validator("model")
    @classmethod
    def exact_nonblank_model(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("model weight requires an exact nonblank inventory label")
        return value


class GpuTimeWeights(PolicyContract):
    default_weight: GpuTimeWeight
    model_weights: tuple[GpuModelWeight, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def unique_models(self) -> "GpuTimeWeights":
        if len({item.model for item in self.model_weights}) != len(self.model_weights):
            raise ValueError("model weight targets must be unique")
        return self


class ReservationRules(PolicyContract):
    max_duration_seconds: PositiveSeconds
    min_notice_seconds: int = Field(ge=0, le=YEAR_SECONDS, strict=True)
    max_advance_seconds: int = Field(ge=0, le=YEAR_SECONDS, strict=True)
    max_concurrent_gpus: ConcurrentGpus

    @model_validator(mode="after")
    def possible_notice(self) -> "ReservationRules":
        if self.min_notice_seconds > self.max_advance_seconds:
            raise ValueError("advance horizon must include minimum notice")
        return self


class RoleLimits(PolicyContract):
    quota_limit: QuotaLimit | None = None
    max_duration_seconds: PositiveSeconds | None = None
    max_concurrent_gpus: ConcurrentGpus | None = None

    @model_validator(mode="after")
    def meaningful_override(self) -> "RoleLimits":
        if all(
            value is None
            for value in (self.quota_limit, self.max_duration_seconds, self.max_concurrent_gpus)
        ):
            raise ValueError("role override must change at least one supported limit")
        return self


class RoleOverride(PolicyContract):
    role: PolicyName
    priority: int = Field(ge=0, le=10000, strict=True)
    limits: RoleLimits


class IdleReclaimDisabled(PolicyContract):
    mode: Literal["disabled"]


class IdleReclaimRules(PolicyContract):
    mode: Literal["enabled"]
    utilization_below_percent: int = Field(ge=0, le=100, strict=True)
    observation_seconds: int = Field(gt=0, le=86400, strict=True)
    grace_seconds: int = Field(ge=0, le=86400, strict=True)
    action: Literal["release_reservation"]


IdleReclaim = Annotated[IdleReclaimDisabled | IdleReclaimRules, Field(discriminator="mode")]


class PolicySpec(PolicyContract):
    timezone: str = Field(min_length=1, max_length=128)
    quota: QuotaRules
    gpu_time_weights: GpuTimeWeights
    reservation: ReservationRules
    idle_reclaim: IdleReclaim
    role_overrides: tuple[RoleOverride, ...] = Field(default=(), max_length=32)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("policy timezone must be a known IANA timezone") from None
        return value

    @model_validator(mode="after")
    def unambiguous_overrides(self) -> "PolicySpec":
        if len({item.role for item in self.role_overrides}) != len(self.role_overrides):
            raise ValueError("role overrides must have unique role names")
        if len({item.priority for item in self.role_overrides}) != len(self.role_overrides):
            raise ValueError("role overrides must have distinct priorities")
        return self


class PolicyMetadata(PolicyContract):
    name: PolicyName
    version: int = Field(ge=1, le=2147483647, strict=True)


class GpuAllocationPolicy(PolicyContract):
    api_version: Literal["aris.policy/v1"]
    kind: Literal["GpuAllocationPolicy"]
    metadata: PolicyMetadata
    spec: PolicySpec
