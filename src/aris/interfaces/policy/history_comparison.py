"""Owner-bound comparison inputs and lossless historical comparison presentation."""

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from aris.core.access.schemas import AccessContract
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.policy.history import (
    HistoricalEvidenceView,
    PolicyHistoricalUsage,
    PolicyHistoryAssessmentView,
    PolicyHistoryReviewInput,
)
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_material_schemas import ImplementationReference
from aris.modules.nova.policy_review import PolicyDifference
from aris.modules.nova.quota_comparison_schemas import (
    HistoricalQuotaComparison,
    HistoricalQuotaComparisonQuery,
)
from aris.modules.nova.quota_comparison_service import HistoricalQuotaComparisonService


def comparison_json(value: HistoricalQuotaComparison) -> str:
    return json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def comparison_fingerprint(value: HistoricalQuotaComparison, implementation: str) -> str:
    return hashlib.sha256(
        b"aris.nova.historical_comparison\x00"
        + implementation.encode()
        + b"\x00"
        + comparison_json(value).encode()
    ).hexdigest()


class PolicyHistoryComparisonInput(PolicyHistoryReviewInput):
    target: PolicySourceReference
    expected_target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def two_versions(self) -> "PolicyHistoryComparisonInput":
        if self.reference.name != self.target.name or self.reference == self.target:
            raise ValueError("historical comparison requires two versions of the same policy")
        return self


class PolicyHistoryComparisonExport(PolicyHistoryComparisonInput):
    expected_context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyHistoryComparisonView(AccessContract):
    comparison_only: Literal[True] = True
    historical_only: Literal[True] = True
    current_dependencies_assessed: Literal[False] = False
    publication_performed: Literal[False] = False
    booking_authorization: Literal[False] = False
    implementation_ref: ImplementationReference
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: PolicyHistoryAssessmentView
    after: PolicyHistoryAssessmentView
    evidence: HistoricalEvidenceView
    difference: PolicyDifference
    quota_state_changed: bool | None = Field(default=None, strict=True)
    canonical_comparison_json: str = Field(min_length=1, max_length=2_097_152, repr=False)

    @property
    def comparison(self) -> HistoricalQuotaComparison:
        return HistoricalQuotaComparison.model_validate_json(self.canonical_comparison_json)

    @model_validator(mode="after")
    def coherent(self) -> "PolicyHistoryComparisonView":
        value = self.comparison
        if (
            self.before != PolicyHistoryAssessmentView.of(value.before)
            or self.after != PolicyHistoryAssessmentView.of(value.after)
            or self.evidence != HistoricalEvidenceView.of(value.evidence)
            or self.difference != value.difference
            or self.quota_state_changed != value.quota_state_changed
            or self.context_fingerprint != comparison_fingerprint(value, self.implementation_ref)
        ):
            raise ValueError("historical comparison view differs from its original input")
        return self

    @classmethod
    def of(
        cls, value: HistoricalQuotaComparison, implementation: str
    ) -> "PolicyHistoryComparisonView":
        return cls(
            implementation_ref=implementation,
            context_fingerprint=comparison_fingerprint(value, implementation),
            before=PolicyHistoryAssessmentView.of(value.before),
            after=PolicyHistoryAssessmentView.of(value.after),
            evidence=HistoricalEvidenceView.of(value.evidence),
            difference=value.difference,
            quota_state_changed=value.quota_state_changed,
            canonical_comparison_json=comparison_json(value),
        )


class PolicyHistoryComparison:
    def __init__(self, history: PolicyHistoricalUsage, implementation: str) -> None:
        self.history = history
        self.implementation = implementation
        self.service = HistoricalQuotaComparisonService(history.assessments)

    def compare(
        self, actor: ActorContext, query: PolicyHistoryComparisonInput
    ) -> PolicyHistoryComparisonView:
        if query.expected_implementation_ref != self.implementation:
            raise DomainError("CONFLICT", "Historical comparison implementation changed", 409)
        value = self.service.compare(
            actor,
            HistoricalQuotaComparisonQuery(
                before=self.history._query(actor, query),
                target=query.target,
                expected_target_fingerprint=query.expected_target_fingerprint,
            ),
        )
        return PolicyHistoryComparisonView.of(value, self.implementation)
