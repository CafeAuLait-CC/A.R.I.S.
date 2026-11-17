from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.nova.policy_simulation import (
    PolicyScenario,
    PolicyScenarioResult,
    simulate_policy_constraints,
)
from aris.modules.nova.policy_source import compile_policy
from aris.modules.nova.policy_units import policy_quota_window
from tests.test_policy_source import node_quota, override, parse, policy

NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def compiled(**changes):
    return compile_policy(parse(policy(**changes)))


def scenario(
    source=None, *, used_microseconds=0, include_usage=True, quality="complete", **changes
):
    source = source or compiled()
    quota = source.source.spec.quota
    raw = {
        "name": "synthetic-only",
        "user_key": "fixture-person",
        "node_id": "fixture-a",
        "as_of": NOW,
        "request": {"start_at": NOW + timedelta(hours=1), "end_at": NOW + timedelta(hours=2)},
        "projected_concurrent_gpus": 1,
    }
    if include_usage:
        raw["usage"] = {
            "quality": quality,
            "user_key": "fixture-person",
            "scope_type": "global" if quota.scope == "global" else "node",
            "scope_id": "*" if quota.scope == "global" else "fixture-a",
            "period": policy_quota_window(source.source.spec, NOW),
            "as_of": NOW,
            "amounts": [{"model": "Fixture GPU", "observed_microseconds": used_microseconds}]
            if used_microseconds
            else [],
        }
    raw.update(changes)
    return PolicyScenario.model_validate(raw)


def quota_result(result):
    return next(item for item in result.results if item.rule_id == "quota.actual_usage")


def test_known_zero_usage_is_explicit_and_only_produces_a_simulated_constraints_result():
    source = compiled()
    case = scenario(source)
    result = simulate_policy_constraints(source, case)
    assert (
        result.decision == "WOULD_ALLOW"
        and result.simulation_only
        and not result.reservation_precharge
    )
    assert result.assumed_used_gpu_seconds.numerator == 0
    assert result.assessment_scope == "policy_constraints_only"
    assert result.source_fingerprint == source.fingerprint and result.source_version == 1
    assert len(result.results) == 5
    assert simulate_policy_constraints(source, case) == result


@pytest.mark.parametrize("quality", ["missing", "unknown", "partial"])
def test_missing_or_incomplete_usage_is_never_treated_as_zero(quality):
    source = compiled()
    result = simulate_policy_constraints(
        source,
        scenario(
            source,
            include_usage=quality != "missing",
            quality="unknown" if quality == "missing" else quality,
        ),
    )
    assert result.decision == "NOT_EVALUATED" and quota_result(result).reason == "USAGE_UNKNOWN"
    assert result.assumed_used_gpu_seconds is None


def test_actual_usage_is_compared_without_precharging_the_future_reservation():
    source = compiled()
    below = simulate_policy_constraints(source, scenario(source, used_microseconds=7199 * 1000000))
    assert below.decision == "WOULD_ALLOW" and not below.reservation_precharge
    exact = simulate_policy_constraints(source, scenario(source, used_microseconds=7200 * 1000000))
    assert exact.decision == "WOULD_DENY" and quota_result(exact).reason == "WEEKLY_QUOTA_EXHAUSTED"


@pytest.mark.parametrize("kind", ["user", "scope", "cutoff", "window"])
def test_usage_must_match_user_scope_period_and_exact_cutoff(kind):
    source = compiled()
    raw = scenario(source).model_dump(mode="json")
    if kind == "user":
        raw["usage"]["user_key"] = "other-person"
    elif kind == "scope":
        raw["usage"].update(scope_type="node", scope_id="fixture-a")
    elif kind == "cutoff":
        raw["usage"]["as_of"] = (NOW - timedelta(seconds=1)).isoformat()
    else:
        raw["usage"]["period"]["start_at"] = (
            policy_quota_window(source.source.spec, NOW).start_at - timedelta(days=1)
        ).isoformat()
    result = simulate_policy_constraints(source, PolicyScenario.model_validate(raw))
    assert result.decision == "NOT_EVALUATED" and result.assumed_used_gpu_seconds is None


def test_node_budgets_are_independent_and_unknown_node_has_no_unlimited_fallback():
    source = compiled(quota=node_quota())
    assert simulate_policy_constraints(source, scenario(source)).decision == "WOULD_ALLOW"
    raw = scenario(source, used_microseconds=604800 * 1000000).model_dump()
    raw["node_id"] = "fixture-b"
    raw["usage"]["scope_id"] = "fixture-b"
    assert (
        simulate_policy_constraints(source, PolicyScenario.model_validate(raw)).decision
        == "WOULD_DENY"
    )
    raw["node_id"] = "fixture-a"
    mismatch = simulate_policy_constraints(source, PolicyScenario.model_validate(raw))
    assert (
        mismatch.decision == "NOT_EVALUATED"
        and quota_result(mismatch).reason == "USAGE_SCOPE_MISMATCH"
    )
    source = compiled(
        quota=node_quota(),
        role_overrides=[override("fixture_admin", 1, quota_limit={"kind": "unlimited"})],
    )
    result = simulate_policy_constraints(
        source,
        scenario(
            source, node_id="unconfigured", assumed_roles=("fixture_admin",), include_usage=False
        ),
    )
    assert result.decision == "NOT_EVALUATED" and result.quota_limit is None
    assert quota_result(result).reason == "QUOTA_SCOPE_UNCONFIGURED"


def test_model_rate_changes_are_applied_to_raw_actual_usage_with_exact_precision():
    weights = {
        "default_weight": {"numerator": 1, "denominator": 1},
        "model_weights": [{"model": "Fixture GPU", "weight": {"numerator": 2, "denominator": 1}}],
    }
    source = compiled(gpu_time_weights=weights)
    result = simulate_policy_constraints(source, scenario(source, used_microseconds=3600 * 1000000))
    assert result.decision == "WOULD_DENY" and result.assumed_used_gpu_seconds.numerator == 7200
    raw = scenario(source, used_microseconds=1).model_dump()
    raw["usage"]["amounts"][0]["model"] = None
    unknown = simulate_policy_constraints(source, PolicyScenario.model_validate(raw))
    assert unknown.decision == "NOT_EVALUATED" and unknown.assumed_used_gpu_seconds is None


@pytest.mark.parametrize("kind", ["duration", "notice", "advance", "concurrent"])
def test_known_static_rejection_is_preserved_even_with_unknown_usage(kind):
    source = compiled()
    raw = scenario(source, include_usage=False).model_dump()
    if kind == "duration":
        raw["request"]["end_at"] += timedelta(microseconds=1)
    elif kind == "notice":
        raw["request"]["start_at"] = NOW + timedelta(seconds=60, microseconds=-1)
        raw["request"]["end_at"] = raw["request"]["start_at"] + timedelta(hours=1)
    elif kind == "advance":
        raw["request"]["start_at"] = NOW + timedelta(days=7, microseconds=1)
        raw["request"]["end_at"] = raw["request"]["start_at"] + timedelta(hours=1)
    else:
        raw["projected_concurrent_gpus"] = 3
    result = simulate_policy_constraints(source, PolicyScenario.model_validate(raw))
    assert result.decision == "WOULD_DENY" and quota_result(result).state == "unknown"


def test_unknown_concurrency_does_not_become_zero_and_role_priority_is_explicit():
    source = compiled()
    assert (
        simulate_policy_constraints(
            source, scenario(source, projected_concurrent_gpus=None)
        ).decision
        == "NOT_EVALUATED"
    )
    source = compiled(
        role_overrides=[
            override("fixture_low", 1, max_concurrent_gpus=3),
            override("fixture_high", 2, max_concurrent_gpus=1),
        ]
    )
    result = simulate_policy_constraints(
        source,
        scenario(
            source, projected_concurrent_gpus=2, assumed_roles=("fixture_low", "fixture_high")
        ),
    )
    assert result.decision == "WOULD_DENY" and result.selected_role_override == "fixture_high"


def test_explicit_unlimited_quota_skips_usage_without_fabricating_numeric_zero():
    source = compiled(
        role_overrides=[override("fixture_unlimited", 1, quota_limit={"kind": "unlimited"})]
    )
    result = simulate_policy_constraints(
        source, scenario(source, assumed_roles=("fixture_unlimited",), include_usage=False)
    )
    assert result.decision == "WOULD_ALLOW" and result.assumed_used_gpu_seconds is None
    assert quota_result(result).reason == "QUOTA_UNLIMITED"


def test_source_or_result_cannot_claim_an_unverified_simulation_decision():
    source = compiled()
    with pytest.raises(DomainError):
        simulate_policy_constraints(replace(source, fingerprint="f" * 64), scenario(source))
    result = simulate_policy_constraints(source, scenario(source, include_usage=False))
    with pytest.raises(ValidationError):
        PolicyScenarioResult.model_validate({**result.model_dump(), "decision": "WOULD_ALLOW"})
    with pytest.raises(ValidationError):
        PolicyScenarioResult.model_validate({**result.model_dump(), "simulation_only": False})


@pytest.mark.parametrize("invalid", [False, 0, -1, "2"])
def test_projected_concurrency_must_include_the_request_and_be_a_strict_positive_integer(invalid):
    with pytest.raises(ValidationError):
        scenario(projected_concurrent_gpus=invalid)
