import copy
import json
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import GpuAllocationPolicy
from aris.modules.nova.policy_source import (
    MAX_POLICY_SOURCE_BYTES,
    compile_policy,
    parse_policy_source,
)


def policy(**spec_changes):
    # Synthetic schema fixture, never a default or a published lab policy.
    return {
        "api_version": "aris.policy/v1",
        "kind": "GpuAllocationPolicy",
        "metadata": {"name": "fixture-only", "version": 1},
        "spec": {
            "timezone": "America/Vancouver",
            "quota": {
                "scope": "global",
                "basis": "actual_gpu_usage",
                "window": {"kind": "calendar_week", "week_starts_on": "monday"},
                "default_limit": {"kind": "limited", "gpu_seconds": 7200},
            },
            "gpu_time_weights": {"default_weight": {"numerator": 1, "denominator": 1}},
            "reservation": {
                "max_duration_seconds": 3600,
                "min_notice_seconds": 60,
                "max_advance_seconds": 604800,
                "max_concurrent_gpus": 2,
            },
            "idle_reclaim": {"mode": "disabled"},
            **spec_changes,
        },
    }


def parse(value):
    return parse_policy_source(json.dumps(value).encode())


def override(role, priority, **limits):
    return {"role": role, "priority": priority, "limits": limits}


def test_strict_format_has_no_activation_or_authorization_result():
    result = compile_policy(parse(policy()))
    assert result.source.spec.quota.default_limit.kind == "limited"
    assert result.source.spec.quota.default_limit.gpu_seconds == 7200
    assert result.source.metadata.version == 1
    assert set(result.__dataclass_fields__) == {"source", "canonical_json", "fingerprint"}
    assert "decision" not in json.loads(result.canonical_json)


@pytest.mark.parametrize(
    "path,value",
    [
        (("execute",), "__import__('os').system('do-not-run')"),
        (("metadata", "status"), "published"),
        (("spec", "priority"), {"recent_usage": -0.6}),
        (("spec", "quota", "query"), "SELECT private data"),
        (("spec", "reservation", "on_conflict"), "kill"),
        (("spec", "idle_reclaim", "script"), "dangerous-command"),
    ],
)
def test_unsupported_operations_and_unknown_fields_are_rejected_at_every_layer(path, value):
    source = policy()
    target = source
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DomainError, match="Invalid policy source"):
        parse(source)


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", -1, 0, 2147483648])
def test_version_requires_a_bounded_positive_integer(bad):
    source = policy()
    source["metadata"]["version"] = bad
    with pytest.raises(DomainError):
        parse(source)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_concurrent_gpus", True),
        ("max_concurrent_gpus", 0),
        ("max_concurrent_gpus", 1001),
        ("max_duration_seconds", 0),
        ("max_duration_seconds", "3600"),
        ("min_notice_seconds", -1),
        ("max_advance_seconds", 59),
    ],
)
def test_reservation_limits_reject_coercion_and_impossible_boundaries(field, bad):
    source = policy()
    source["spec"]["reservation"][field] = bad
    with pytest.raises(DomainError):
        parse(source)


@pytest.mark.parametrize("bad", [True, -1, 0.5, "7200", 1000 * 604800 + 1, None])
def test_quota_is_exact_integer_gpu_seconds_and_unknown_is_not_zero(bad):
    source = policy()
    source["spec"]["quota"]["default_limit"]["gpu_seconds"] = bad
    with pytest.raises(DomainError):
        parse(source)


def test_explicit_zero_unlimited_and_missing_are_different():
    source = policy()
    source["spec"]["quota"]["default_limit"]["gpu_seconds"] = 0
    assert parse(source).spec.quota.default_limit.gpu_seconds == 0
    source["spec"]["quota"]["default_limit"] = {"kind": "unlimited"}
    assert parse(source).spec.quota.default_limit.kind == "unlimited"
    source["spec"]["quota"]["default_limit"]["gpu_seconds"] = 0
    with pytest.raises(DomainError):
        parse(source)
    del source["spec"]["quota"]["default_limit"]
    with pytest.raises(DomainError):
        parse(source)


@pytest.mark.parametrize(
    "window",
    [
        {"kind": "weekly"},
        {"kind": "calendar_week"},
        {"kind": "calendar_week", "week_starts_on": "someday"},
        {"kind": "rolling_7d", "week_starts_on": "monday"},
    ],
)
def test_week_window_semantics_must_be_explicit(window):
    source = policy()
    source["spec"]["quota"]["window"] = window
    with pytest.raises(DomainError):
        parse(source)


@pytest.mark.parametrize("zone", ["+08:00", "not/a-timezone", "", "../UTC"])
def test_unknown_timezone_is_not_silently_replaced(zone):
    with pytest.raises(DomainError):
        parse(policy(timezone=zone))


def test_roles_require_distinct_names_priorities_and_a_meaningful_supported_override():
    valid = override("fixture_researcher", 10, max_concurrent_gpus=3)
    assert parse(policy(role_overrides=[valid])).spec.role_overrides[0].limits.quota_limit is None
    for values in (
        [valid, valid],
        [valid, override("fixture_other", 10, max_duration_seconds=7200)],
        [override("fixture_other", 20)],
        [override("*", 20, max_concurrent_gpus=3)],
        [override("fixture_other", True, max_concurrent_gpus=3)],
        [override("fixture_other", 20, node_command="kill")],
    ):
        with pytest.raises(DomainError):
            parse(policy(role_overrides=values))


def test_role_budget_is_bounded():
    values = [
        override("fixture_" + str(index), index, max_concurrent_gpus=3) for index in range(33)
    ]
    with pytest.raises(DomainError):
        parse(policy(role_overrides=values))


def test_idle_configuration_cannot_select_a_process_action_or_zero_observation():
    idle = {
        "mode": "enabled",
        "utilization_below_percent": 5,
        "observation_seconds": 1800,
        "grace_seconds": 900,
        "action": "release_reservation",
    }
    assert parse(policy(idle_reclaim=idle)).spec.idle_reclaim.mode == "enabled"
    for changes in (
        {"action": "kill_process"},
        {"observation_seconds": 0},
        {"utilization_below_percent": True},
    ):
        with pytest.raises(DomainError):
            parse(policy(idle_reclaim={**idle, **changes}))


@pytest.mark.parametrize(
    "body",
    [
        b'{"kind":"GpuAllocationPolicy","kind":"Other"}',
        b'{"spec":{"quota":{"default_limit":{"gpu_seconds":1,"gpu_seconds":2}}}}',
        b'{"x": NaN}',
        b'{"x": Infinity}',
        b'{"x": -Infinity}',
        b"[]",
        b"null",
        b"\xff",
        b"{",
        b"",
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_ambiguous_invalid_and_deep_json_is_rejected_without_echoing_values(body):
    with pytest.raises(DomainError) as failure:
        parse_policy_source(body)
    assert failure.value.code == "VALIDATION_ERROR" and failure.value.status == 422
    assert "gpu_seconds" not in failure.value.message and "NaN" not in failure.value.message


def test_source_size_budget_is_checked_before_json_parsing():
    with pytest.raises(DomainError, match="size budget"):
        parse_policy_source(b" " * (MAX_POLICY_SOURCE_BYTES + 1))


def test_compilation_is_deterministic_for_equivalent_json_and_override_order():
    a = override("fixture_low", 10, max_duration_seconds=7200)
    b = override("fixture_high", 20, quota_limit={"kind": "unlimited"})
    source = policy(role_overrides=[a, b])
    first = compile_policy(parse(source))
    other = copy.deepcopy(source)
    other["spec"]["role_overrides"].reverse()
    second = compile_policy(
        parse_policy_source(json.dumps(other, sort_keys=True, indent=4).encode())
    )
    assert first == second
    assert [rule.priority for rule in first.source.spec.role_overrides] == [20, 10]
    assert compile_policy(parse_policy_source(first.canonical_json)) == first
    assert len(first.fingerprint) == 64
    other["spec"]["quota"]["default_limit"]["gpu_seconds"] += 1
    assert compile_policy(parse(other)).fingerprint != first.fingerprint


def test_source_and_compiled_tree_are_immutable_and_compiler_revalidates_unchecked_models():
    source = parse(policy())
    with pytest.raises(ValidationError):
        source.spec.reservation.max_concurrent_gpus = 999
    compiled = compile_policy(source)
    with pytest.raises(FrozenInstanceError):
        compiled.fingerprint = "unapproved"
    unchecked = source.model_copy(
        update={"spec": source.spec.model_copy(update={"timezone": "invalid/zone"})}
    )
    with pytest.raises(ValidationError):
        compile_policy(unchecked)
    assert isinstance(source, GpuAllocationPolicy)


def node_quota():
    return {
        "scope": "per_node",
        "basis": "actual_gpu_usage",
        "window": {"kind": "calendar_week", "week_starts_on": "monday"},
        "node_limits": [
            {"node_id": "fixture-b", "limit": {"kind": "limited", "gpu_seconds": 604800}},
            {"node_id": "fixture-a", "limit": {"kind": "limited", "gpu_seconds": 604800}},
        ],
    }


def test_per_node_quota_is_explicit_and_has_no_fallback_for_other_nodes():
    result = compile_policy(parse(policy(quota=node_quota())))
    assert result.source.spec.quota.scope == "per_node"
    assert [item.node_id for item in result.source.spec.quota.node_limits] == [
        "fixture-a",
        "fixture-b",
    ]
    assert not hasattr(result.source.spec.quota, "default_limit")
    other = node_quota()
    other["node_limits"].reverse()
    assert compile_policy(parse(policy(quota=other))) == result


@pytest.mark.parametrize(
    "mutation", ["duplicate_node", "empty", "global_default", "reservation_basis", "unknown_scope"]
)
def test_node_quotas_cannot_silently_merge_or_change_actual_usage_basis(mutation):
    quota = node_quota()
    if mutation == "duplicate_node":
        quota["node_limits"].append(quota["node_limits"][0].copy())
    elif mutation == "empty":
        quota["node_limits"] = []
    elif mutation == "global_default":
        quota["default_limit"] = {"kind": "unlimited"}
    elif mutation == "reservation_basis":
        quota["basis"] = "reserved_gpu_time"
    else:
        quota["scope"] = "infer_from_roles"
    with pytest.raises(DomainError):
        parse(policy(quota=quota))


def test_model_time_weights_use_exact_rationals_and_canonical_model_order():
    weights = {
        "default_weight": {"numerator": 2, "denominator": 2},
        "model_weights": [
            {"model": "Fixture GPU B", "weight": {"numerator": 6, "denominator": 4}},
            {"model": "Fixture GPU A", "weight": {"numerator": 3, "denominator": 3}},
        ],
    }
    result = compile_policy(parse(policy(gpu_time_weights=weights)))
    actual = result.source.spec.gpu_time_weights
    assert actual.default_weight.model_dump() == {"numerator": 1, "denominator": 1}
    assert [item.model for item in actual.model_weights] == ["Fixture GPU A", "Fixture GPU B"]
    assert actual.model_weights[1].weight.model_dump() == {"numerator": 3, "denominator": 2}
    equivalent = copy.deepcopy(weights)
    equivalent["default_weight"] = {"numerator": 1, "denominator": 1}
    equivalent["model_weights"][0]["weight"] = {"numerator": 3, "denominator": 2}
    equivalent["model_weights"].reverse()
    assert compile_policy(parse(policy(gpu_time_weights=equivalent))) == result


@pytest.mark.parametrize(
    "weight",
    [
        {"numerator": True, "denominator": 1},
        {"numerator": 1.5, "denominator": 1},
        {"numerator": 0, "denominator": 1},
        {"numerator": 1, "denominator": 0},
        {"numerator": 1001, "denominator": 1},
    ],
)
def test_time_weights_reject_coercion_zero_and_unbounded_values(weight):
    with pytest.raises(DomainError):
        parse(policy(gpu_time_weights={"default_weight": weight}))


def test_model_rate_targets_must_be_unique_exact_inventory_labels():
    item = {"model": "Fixture GPU", "weight": {"numerator": 1, "denominator": 1}}
    for models in ([item, item], [{**item, "model": " Fixture GPU"}], [{**item, "model": " "}]):
        with pytest.raises(DomainError):
            parse(
                policy(gpu_time_weights={"default_weight": item["weight"], "model_weights": models})
            )
