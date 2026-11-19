"""Read projection keeps example authority explicit and large fractions exact over JSON."""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.interfaces.policy.history import ExactSecondsView
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_material_schemas import (
    ImplementationReference,
    PolicyMaterialSummary,
    RecordPolicyReviewMaterial,
    StoredPolicyReviewMaterial,
)
from aris.modules.nova.policy_review import PolicyDifference
from aris.modules.nova.policy_schemas import QuotaLimit
from aris.modules.nova.policy_simulation import (
    ConstraintResult,
    PolicyScenario,
    PolicyScenarioResult,
)


class PolicyMaterialDetailInput(AccessContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    id: UUID
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyExampleResultView(AccessContract):
    simulation_only: Literal[True] = True
    reservation_precharge: Literal[False] = False
    scenario_name: str
    decision: Literal["WOULD_ALLOW", "WOULD_DENY", "NOT_EVALUATED"]
    selected_role_override: str | None
    results: tuple[ConstraintResult, ...]
    quota_limit: QuotaLimit | None
    assumed_used_gpu_seconds: ExactSecondsView | None

    @classmethod
    def of(cls, value: PolicyScenarioResult) -> "PolicyExampleResultView":
        return cls(
            scenario_name=value.scenario_name,
            decision=value.decision,
            selected_role_override=value.selected_role_override,
            results=value.results,
            quota_limit=value.quota_limit,
            assumed_used_gpu_seconds=ExactSecondsView.of(value.assumed_used_gpu_seconds),
        )


class PolicyMaterialDetailView(AccessContract):
    review_material_only: Literal[True] = True
    basis: Literal["caller_supplied_examples"] = "caller_supplied_examples"
    current_dependencies_assessed: Literal[False] = False
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    summary: PolicyMaterialSummary
    reason: str
    created_by: UUID
    command_id: UUID
    scenarios: tuple[PolicyScenario, ...]
    results: tuple[PolicyExampleResultView, ...]
    raw_scenarios: tuple[str, ...]

    @classmethod
    def of(cls, value: StoredPolicyReviewMaterial) -> "PolicyMaterialDetailView":
        request = value.request
        return cls(
            summary=PolicyMaterialSummary(
                id=value.id,
                source=request.source,
                source_id=value.source_id,
                source_fingerprint=request.expected_source_fingerprint,
                implementation_ref=value.implementation_ref,
                scenarios=len(request.scenarios),
                created_at=value.created_at,
                fingerprint=value.fingerprint,
            ),
            reason=request.reason,
            created_by=value.created_by,
            command_id=value.command_id,
            scenarios=request.scenarios,
            results=tuple(PolicyExampleResultView.of(result) for result in value.results),
            raw_scenarios=tuple(case.model_dump_json(indent=2) for case in request.scenarios),
        )


class PolicyMaterialComparisonInput(PolicyMaterialDetailInput):
    target: PolicySourceReference
    expected_target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_implementation_ref: ImplementationReference

    @model_validator(mode="after")
    def one_policy(self) -> "PolicyMaterialComparisonInput":
        if self.source.name != self.target.name:
            raise ValueError("stored material comparison requires the same policy name")
        return self


class PolicyMaterialComparedCase(AccessContract):
    scenario: PolicyScenario
    raw_scenario: str
    recorded_result: PolicyExampleResultView
    before_result: PolicyExampleResultView
    after_result: PolicyExampleResultView
    stored_replay_matches: bool
    decision_changed: bool
    changed_rules: tuple[str, ...]


class PolicyMaterialComparisonView(AccessContract):
    comparison_only: Literal[True] = True
    basis: Literal["stored_caller_supplied_examples"] = "stored_caller_supplied_examples"
    current_dependencies_assessed: Literal[False] = False
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    material: PolicyMaterialSummary
    comparison_implementation_ref: ImplementationReference
    target: PolicySourceReference
    target_source_id: UUID
    target_source_fingerprint: str
    difference: PolicyDifference
    cases: tuple[PolicyMaterialComparedCase, ...] = Field(min_length=1, max_length=8)


class PolicyMaterialValidation(AccessContract):
    review_material_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    current_dependencies_assessed: Literal[False] = False
    request: RecordPolicyReviewMaterial
    results: tuple[PolicyExampleResultView, ...]
    inline_bytes: int = Field(ge=1, le=16384)


def _unique_example_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate example field")
        result[name] = value
    return result


def _reject_example_number(value: str) -> None:
    raise ValueError("example numeric fields require integers")


class PolicyMaterialTextInput(AccessContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_implementation_ref: ImplementationReference
    scenarios_text: str = Field(min_length=1, max_length=16384, repr=False)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    def parsed(self) -> RecordPolicyReviewMaterial:
        """Preserve integer inputs; reject duplicates before browser normalization."""
        try:
            if len(self.scenarios_text.encode("utf-8")) > 16384:
                raise ValueError("example text exceeds byte budget")
            scenarios = json.loads(
                self.scenarios_text,
                object_pairs_hook=_unique_example_fields,
                parse_float=_reject_example_number,
                parse_constant=_reject_example_number,
            )
            return RecordPolicyReviewMaterial.model_validate(
                {
                    "source": self.source,
                    "expected_source_fingerprint": self.expected_source_fingerprint,
                    "expected_implementation_ref": self.expected_implementation_ref,
                    "scenarios": scenarios,
                    "reason": self.reason,
                }
            )
        except (ValueError, RecursionError):
            raise DomainError(
                "VALIDATION_ERROR", "Invalid hypothetical example JSON", 422
            ) from None
