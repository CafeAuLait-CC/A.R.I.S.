"""Constraint scenarios with supplied assumptions; never an actual policy authorization."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import (
    YEAR_SECONDS,
    GlobalQuotaRules,
    PolicyContract,
    PolicyName,
    QuotaLimit,
)
from aris.modules.nova.policy_source import CompiledPolicy, compile_policy
from aris.modules.nova.policy_units import (
    PolicyTimeWindow,
    policy_quota_window,
    weighted_gpu_seconds,
)


class ModelUsageAmount(PolicyContract):
    model: str | None = Field(default=None, min_length=1, max_length=256)
    observed_microseconds: int = Field(ge=0, le=YEAR_SECONDS * 1000000, strict=True)


class ScenarioUsage(PolicyContract):
    quality: Literal["complete", "partial", "unknown"]
    user_key: str = Field(min_length=1, max_length=128)
    scope_type: Literal["global", "node"]
    scope_id: str = Field(min_length=1, max_length=128)
    period: PolicyTimeWindow
    as_of: AwareDatetime
    amounts: tuple[ModelUsageAmount, ...] = Field(default=(), max_length=64)

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_snapshot(self) -> "ScenarioUsage":
        if (
            self.scope_type == "global"
            and self.scope_id != "*"
            or self.scope_type == "node"
            and self.scope_id == "*"
        ):
            raise ValueError("usage scope differs")
        if not self.period.start_at <= self.as_of <= self.period.end_at:
            raise ValueError("usage cutoff is outside its period")
        if len({item.model for item in self.amounts}) != len(self.amounts):
            raise ValueError("scenario usage must be aggregated per model")
        return self


class PolicyScenario(PolicyContract):
    name: str = Field(min_length=1, max_length=64)
    user_key: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    as_of: AwareDatetime
    request: PolicyTimeWindow
    projected_concurrent_gpus: int | None = Field(default=None, ge=1, le=1000, strict=True)
    assumed_roles: tuple[PolicyName, ...] = Field(default=(), max_length=32)
    usage: ScenarioUsage | None = None

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def unique_roles(self) -> "PolicyScenario":
        if len(set(self.assumed_roles)) != len(self.assumed_roles):
            raise ValueError("assumed roles must be unique")
        return self


class ConstraintResult(PolicyContract):
    rule_id: Literal[
        "reservation.max_duration",
        "reservation.min_notice",
        "reservation.max_advance",
        "reservation.concurrent",
        "quota.actual_usage",
    ]
    state: Literal["pass", "deny", "unknown"]
    reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)


class ExactGpuSeconds(PolicyContract):
    numerator: int = Field(ge=0, strict=True)
    denominator: int = Field(ge=1, strict=True)


class PolicyScenarioResult(PolicyContract):
    simulation_only: Literal[True] = True
    assessment_scope: Literal["policy_constraints_only"] = "policy_constraints_only"
    reservation_precharge: Literal[False] = False
    policy_name: str
    source_version: int = Field(ge=1, strict=True)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    scenario_name: str
    decision: Literal["WOULD_ALLOW", "WOULD_DENY", "NOT_EVALUATED"]
    selected_role_override: str | None = None
    results: tuple[ConstraintResult, ...] = Field(min_length=5, max_length=5)
    quota_limit: QuotaLimit | None = None
    assumed_used_gpu_seconds: ExactGpuSeconds | None = None

    @model_validator(mode="after")
    def honest_decision(self) -> "PolicyScenarioResult":
        expected_rules = {
            "reservation.max_duration",
            "reservation.min_notice",
            "reservation.max_advance",
            "reservation.concurrent",
            "quota.actual_usage",
        }
        if {item.rule_id for item in self.results} != expected_rules:
            raise ValueError("scenario must contain every supported constraint exactly once")
        expected = (
            "WOULD_DENY"
            if any(item.state == "deny" for item in self.results)
            else (
                "NOT_EVALUATED"
                if any(item.state == "unknown" for item in self.results)
                else "WOULD_ALLOW"
            )
        )
        if self.decision != expected:
            raise ValueError("scenario decision differs from constraint outcomes")
        quota = next(item for item in self.results if item.rule_id == "quota.actual_usage")
        if quota.state == "unknown" and self.assumed_used_gpu_seconds is not None:
            raise ValueError("unknown usage cannot contain a numeric usage total")
        return self


def _microseconds(delta: timedelta) -> int:
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def simulate_policy_constraints(
    compiled: CompiledPolicy, scenario: PolicyScenario
) -> PolicyScenarioResult:
    canonical = compile_policy(compiled.source)
    scenario = PolicyScenario.model_validate(scenario.model_dump(mode="json"))
    if (
        canonical.fingerprint != compiled.fingerprint
        or canonical.canonical_json != compiled.canonical_json
    ):
        raise DomainError("VALIDATION_ERROR", "Compiled source identity differs", 422)
    spec = canonical.source.spec
    selected = next(
        (item for item in spec.role_overrides if item.role in scenario.assumed_roles), None
    )
    # Source compilation puts the highest explicit priority first.
    max_duration = (
        selected.limits.max_duration_seconds
        if selected and selected.limits.max_duration_seconds is not None
        else spec.reservation.max_duration_seconds
    )
    max_concurrent = (
        selected.limits.max_concurrent_gpus
        if selected and selected.limits.max_concurrent_gpus is not None
        else spec.reservation.max_concurrent_gpus
    )
    results = []
    duration = _microseconds(scenario.request.end_at - scenario.request.start_at)
    notice = _microseconds(scenario.request.start_at - scenario.as_of)
    for rule, accepted, reason in (
        ("reservation.max_duration", duration <= max_duration * 1000000, "MAX_DURATION_EXCEEDED"),
        (
            "reservation.min_notice",
            notice >= spec.reservation.min_notice_seconds * 1000000,
            "MIN_NOTICE_UNMET",
        ),
        (
            "reservation.max_advance",
            notice <= spec.reservation.max_advance_seconds * 1000000,
            "ADVANCE_HORIZON_EXCEEDED",
        ),
    ):
        results.append(
            ConstraintResult(
                rule_id=rule,
                state="pass" if accepted else "deny",
                reason="WITHIN_LIMIT" if accepted else reason,
            )
        )
    concurrent = scenario.projected_concurrent_gpus
    results.append(
        ConstraintResult(
            rule_id="reservation.concurrent",
            state="unknown"
            if concurrent is None
            else "pass"
            if concurrent <= max_concurrent
            else "deny",
            reason="CONCURRENCY_UNKNOWN"
            if concurrent is None
            else "WITHIN_LIMIT"
            if concurrent <= max_concurrent
            else "CONCURRENCY_LIMIT_EXCEEDED",
        )
    )
    limit: QuotaLimit | None
    if isinstance(spec.quota, GlobalQuotaRules):
        limit = spec.quota.default_limit
        scope = ("global", "*")
    else:
        limit = next(
            (item.limit for item in spec.quota.node_limits if item.node_id == scenario.node_id),
            None,
        )
        scope = ("node", scenario.node_id)
    if limit is not None and selected and selected.limits.quota_limit is not None:
        limit = selected.limits.quota_limit
    used = None
    if limit is None:
        quota = ConstraintResult(
            rule_id="quota.actual_usage", state="unknown", reason="QUOTA_SCOPE_UNCONFIGURED"
        )
    elif limit.kind == "unlimited":
        quota = ConstraintResult(
            rule_id="quota.actual_usage", state="pass", reason="QUOTA_UNLIMITED"
        )
    else:
        snapshot = scenario.usage
        usage_reason: str | None = None
        if snapshot is None or snapshot.quality != "complete":
            usage_reason = "USAGE_UNKNOWN"
        elif (
            snapshot.user_key != scenario.user_key
            or (snapshot.scope_type, snapshot.scope_id) != scope
        ):
            usage_reason = "USAGE_SCOPE_MISMATCH"
        elif snapshot.as_of != scenario.as_of:
            usage_reason = "USAGE_CUTOFF_MISMATCH"
        else:
            try:
                period = policy_quota_window(spec, scenario.as_of)
                if snapshot.period != period:
                    usage_reason = "USAGE_WINDOW_MISMATCH"
                else:
                    used = sum(
                        (
                            weighted_gpu_seconds(
                                item.observed_microseconds, item.model, spec.gpu_time_weights
                            )
                            for item in snapshot.amounts
                        ),
                        Fraction(),
                    )
            except DomainError as error:
                if error.code != "NOT_EVALUATED":
                    raise
                usage_reason = "USAGE_CONTEXT_UNAVAILABLE"
        if usage_reason is not None:
            quota = ConstraintResult(
                rule_id="quota.actual_usage", state="unknown", reason=usage_reason
            )
        else:
            assert used is not None
            accepted = used < limit.gpu_seconds
            quota = ConstraintResult(
                rule_id="quota.actual_usage",
                state="pass" if accepted else "deny",
                reason="QUOTA_NOT_EXHAUSTED" if accepted else "WEEKLY_QUOTA_EXHAUSTED",
            )
    results.append(quota)
    decision = (
        "WOULD_DENY"
        if any(item.state == "deny" for item in results)
        else "NOT_EVALUATED"
        if any(item.state == "unknown" for item in results)
        else "WOULD_ALLOW"
    )
    return PolicyScenarioResult(
        policy_name=canonical.source.metadata.name,
        source_version=canonical.source.metadata.version,
        source_fingerprint=canonical.fingerprint,
        scenario_name=scenario.name,
        decision=decision,
        selected_role_override=selected.role if selected else None,
        results=tuple(results),
        quota_limit=limit,
        assumed_used_gpu_seconds=ExactGpuSeconds(
            numerator=used.numerator, denominator=used.denominator
        )
        if used is not None
        else None,
    )
