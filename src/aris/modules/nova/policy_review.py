"""Pure semantic source differences and supplied-case comparisons; no publication path."""

import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field, JsonValue, TypeAdapter

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import PolicyContract
from aris.modules.nova.policy_simulation import (
    PolicyScenario,
    PolicyScenarioResult,
    simulate_policy_constraints,
)
from aris.modules.nova.policy_source import CompiledPolicy, compile_policy

MAX_POLICY_CHANGES = 1024
MAX_POLICY_SCENARIOS = 100
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class PolicySourceChange(PolicyContract):
    path: str = Field(min_length=1, max_length=1024, pattern=r"^/")
    before_exists: bool = Field(strict=True)
    after_exists: bool = Field(strict=True)
    before: JsonValue | None = Field(default=None, repr=False)
    after: JsonValue | None = Field(default=None, repr=False)


class PolicyDifference(PolicyContract):
    comparison_only: Literal[True] = True
    policy_name: str
    before_fingerprint: str
    after_fingerprint: str
    changes: tuple[PolicySourceChange, ...] = Field(max_length=MAX_POLICY_CHANGES)


class PolicyCaseComparison(PolicyContract):
    scenario_name: str
    before: PolicyScenarioResult
    after: PolicyScenarioResult
    decision_changed: bool
    changed_rules: tuple[str, ...] = Field(max_length=5)
    evaluated_context_changed: bool


class PolicyScenarioComparison(PolicyContract):
    comparison_only: Literal[True] = True
    difference: PolicyDifference
    cases: tuple[PolicyCaseComparison, ...] = Field(min_length=1, max_length=MAX_POLICY_SCENARIOS)


def _validated(source: CompiledPolicy) -> CompiledPolicy:
    canonical = compile_policy(source.source)
    if (canonical.fingerprint, canonical.canonical_json) != (
        source.fingerprint,
        source.canonical_json,
    ):
        raise DomainError("VALIDATION_ERROR", "Compiled policy source identity differs", 422)
    return canonical


def _semantic_view(source: CompiledPolicy) -> dict[str, JsonValue]:
    value: dict[str, Any] = json.loads(source.canonical_json)
    spec = value["spec"]
    if spec["quota"]["scope"] == "per_node":
        spec["quota"]["node_limits"] = {
            item["node_id"]: item["limit"] for item in spec["quota"]["node_limits"]
        }
    spec["gpu_time_weights"]["model_weights"] = {
        item["model"]: item["weight"] for item in spec["gpu_time_weights"]["model_weights"]
    }
    spec["role_overrides"] = {
        item["role"]: {"priority": item["priority"], "limits": item["limits"]}
        for item in spec["role_overrides"]
    }
    return JSON_OBJECT.validate_python(value, strict=True)


def _flatten(value: JsonValue, path: str = "") -> dict[str, JsonValue]:
    if isinstance(value, dict):
        result = {}
        for key, part in value.items():
            component = key.replace("~", "~0").replace("/", "~1")
            result.update(_flatten(part, path + "/" + component))
        return result
    return {path: value}


def policy_difference(before: CompiledPolicy, after: CompiledPolicy) -> PolicyDifference:
    old, new = _validated(before), _validated(after)
    if old.source.metadata.name != new.source.metadata.name:
        raise DomainError(
            "VALIDATION_ERROR", "Policy comparison requires the same policy name", 422
        )
    a, b = _flatten(_semantic_view(old)), _flatten(_semantic_view(new))
    changes = []
    for path in sorted(set(a) | set(b)):
        if (path in a) != (path in b) or a.get(path) != b.get(path):
            changes.append(
                PolicySourceChange(
                    path=path,
                    before_exists=path in a,
                    after_exists=path in b,
                    before=a.get(path),
                    after=b.get(path),
                )
            )
            if len(changes) > MAX_POLICY_CHANGES:
                raise DomainError(
                    "VALIDATION_ERROR", "Policy difference exceeds its size budget", 422
                )
    return PolicyDifference(
        policy_name=old.source.metadata.name,
        before_fingerprint=old.fingerprint,
        after_fingerprint=new.fingerprint,
        changes=tuple(changes),
    )


def compare_policy_scenarios(
    before: CompiledPolicy, after: CompiledPolicy, scenarios: Sequence[PolicyScenario]
) -> PolicyScenarioComparison:
    if not 1 <= len(scenarios) <= MAX_POLICY_SCENARIOS or len(
        {case.name for case in scenarios}
    ) != len(scenarios):
        raise DomainError("VALIDATION_ERROR", "Policy scenarios require bounded unique names", 422)
    difference = policy_difference(before, after)
    cases = []
    for scenario in scenarios:
        a, b = (
            simulate_policy_constraints(before, scenario),
            simulate_policy_constraints(after, scenario),
        )
        changed = tuple(
            old.rule_id for old, new in zip(a.results, b.results, strict=True) if old != new
        )
        context_changed = (a.quota_limit, a.assumed_used_gpu_seconds, a.selected_role_override) != (
            b.quota_limit,
            b.assumed_used_gpu_seconds,
            b.selected_role_override,
        )
        cases.append(
            PolicyCaseComparison(
                scenario_name=scenario.name,
                before=a,
                after=b,
                decision_changed=a.decision != b.decision,
                changed_rules=changed,
                evaluated_context_changed=context_changed,
            )
        )
    return PolicyScenarioComparison(difference=difference, cases=tuple(cases))
