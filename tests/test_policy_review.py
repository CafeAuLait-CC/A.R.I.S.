import copy
from dataclasses import replace

import pytest

from aris.core.contracts import DomainError
from aris.modules.nova.policy_review import (
    MAX_POLICY_SCENARIOS,
    compare_policy_scenarios,
    policy_difference,
)
from aris.modules.nova.policy_source import compile_policy
from tests.test_policy_simulation import compiled, scenario
from tests.test_policy_source import node_quota, override, parse, policy


def test_equivalent_canonical_sources_and_ratio_order_have_no_semantic_diff():
    a = compiled()
    raw = policy()
    raw["spec"]["gpu_time_weights"]["default_weight"] = {"numerator": 2, "denominator": 2}
    result = policy_difference(a, compile_policy(parse(raw)))
    assert result.comparison_only and result.changes == ()


def test_node_quota_changes_are_keyed_by_node_identity_without_array_index_noise():
    old = node_quota()
    new = copy.deepcopy(old)
    new["node_limits"].reverse()
    new["node_limits"][0]["limit"]["gpu_seconds"] = 7200
    result = policy_difference(compiled(quota=old), compiled(quota=new))
    assert len(result.changes) == 1
    assert result.changes[0].path == "/spec/quota/node_limits/fixture-a/gpu_seconds"
    assert result.changes[0].before == 604800 and result.changes[0].after == 7200


def test_role_inheritance_null_and_new_values_are_distinct_and_targets_are_semantic_keys():
    old = compiled(role_overrides=[override("fixture_role", 1, max_duration_seconds=7200)])
    new = compiled(
        role_overrides=[
            override("fixture_role", 1, max_duration_seconds=7200, max_concurrent_gpus=3)
        ]
    )
    result = policy_difference(old, new)
    change = result.changes[0]
    assert change.path == "/spec/role_overrides/fixture_role/limits/max_concurrent_gpus"
    assert change.before_exists and change.before is None and change.after == 3


def test_model_labels_are_json_pointer_escaped_and_new_targets_are_explicit_additions():
    old = compiled()
    new = compiled(
        gpu_time_weights={
            "default_weight": {"numerator": 1, "denominator": 1},
            "model_weights": [
                {"model": "Fixture/GPU~A", "weight": {"numerator": 3, "denominator": 2}}
            ],
        }
    )
    changes = policy_difference(old, new).changes
    assert {change.path for change in changes} == {
        "/spec/gpu_time_weights/model_weights/Fixture~1GPU~0A/denominator",
        "/spec/gpu_time_weights/model_weights/Fixture~1GPU~0A/numerator",
    }
    assert all(not change.before_exists and change.after_exists for change in changes)


def test_version_only_change_does_not_falsely_report_changed_scenario_decisions():
    old = compiled()
    raw = policy()
    raw["metadata"]["version"] = 2
    new = compile_policy(parse(raw))
    result = compare_policy_scenarios(old, new, [scenario(old)])
    assert [change.path for change in result.difference.changes] == ["/metadata/version"]
    case = result.cases[0]
    assert (
        not case.decision_changed and not case.changed_rules and not case.evaluated_context_changed
    )
    assert case.before.simulation_only and case.after.source_version == 2


def test_changed_limits_identify_affected_scenario_without_publishing_or_changing_input():
    old = compiled()
    raw = policy()
    raw["spec"]["quota"]["default_limit"]["gpu_seconds"] = 3600
    new = compile_policy(parse(raw))
    sample = scenario(old, used_microseconds=3600 * 1000000)
    before = sample.model_dump()
    result = compare_policy_scenarios(old, new, [sample])
    case = result.cases[0]
    assert result.comparison_only and case.decision_changed
    assert case.before.decision == "WOULD_ALLOW" and case.after.decision == "WOULD_DENY"
    assert case.changed_rules == ("quota.actual_usage",)
    assert sample.model_dump() == before


def test_new_time_window_requires_matching_usage_context_instead_of_reusing_old_week_totals():
    old = compiled()
    new = compiled(timezone="Asia/Tokyo")
    result = compare_policy_scenarios(old, new, [scenario(old)])
    assert result.cases[0].after.decision == "NOT_EVALUATED"
    assert result.cases[0].decision_changed


@pytest.mark.parametrize("kind", ["name", "fingerprint", "empty", "duplicate", "oversized"])
def test_invalid_or_unbounded_reviews_fail_without_partial_results(kind):
    old = compiled()
    new = old
    cases = [scenario(old)]
    if kind == "name":
        raw = policy()
        raw["metadata"]["name"] = "other-policy"
        new = compile_policy(parse(raw))
    elif kind == "fingerprint":
        new = replace(old, fingerprint="0" * 64)
    elif kind == "empty":
        cases = []
    elif kind == "duplicate":
        cases = cases * 2
    else:
        cases = [
            scenario(old, name="case-" + str(index)) for index in range(MAX_POLICY_SCENARIOS + 1)
        ]
    with pytest.raises(DomainError):
        compare_policy_scenarios(old, new, cases)


def test_large_valid_sources_cannot_silently_truncate_the_difference():
    def large_source(prefix):
        quota = node_quota()
        quota["node_limits"] = [
            {
                "node_id": prefix + "-node-" + str(index),
                "limit": {"kind": "limited", "gpu_seconds": 7200},
            }
            for index in range(128)
        ]
        return compiled(
            quota=quota,
            gpu_time_weights={
                "default_weight": {"numerator": 1, "denominator": 1},
                "model_weights": [
                    {
                        "model": prefix + " GPU " + str(index),
                        "weight": {"numerator": 2, "denominator": 1},
                    }
                    for index in range(64)
                ],
            },
            role_overrides=[
                override(
                    prefix + "_role_" + str(index),
                    index,
                    quota_limit={"kind": "limited", "gpu_seconds": 3600},
                    max_duration_seconds=7200,
                    max_concurrent_gpus=3,
                )
                for index in range(32)
            ],
        )

    with pytest.raises(DomainError, match="size budget"):
        policy_difference(large_source("before"), large_source("after"))
