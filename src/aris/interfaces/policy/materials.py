"""Read projection keeps example authority explicit and large fractions exact over JSON."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from aris.core.access.schemas import AccessContract
from aris.interfaces.policy.history import ExactSecondsView
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_material_schemas import (
    PolicyMaterialSummary,
    RecordPolicyReviewMaterial,
    StoredPolicyReviewMaterial,
)
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
        )


class PolicyMaterialValidation(AccessContract):
    review_material_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    current_dependencies_assessed: Literal[False] = False
    request: RecordPolicyReviewMaterial
    results: tuple[PolicyExampleResultView, ...]
    inline_bytes: int = Field(ge=1, le=16384)
