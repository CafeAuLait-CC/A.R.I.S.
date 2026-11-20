"""Two draft policies evaluated against one unchanged historical evidence selection."""

from typing import Literal

from pydantic import Field, model_validator

from aris.modules.lyra.quota_evidence_schemas import HistoricalQuotaEvidence
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_review import PolicyDifference
from aris.modules.nova.policy_schemas import PolicyContract
from aris.modules.nova.quota_history_schemas import (
    HistoricalQuotaAssessment,
    HistoricalQuotaQuery,
    HistoricalQuotaReview,
)


class HistoricalQuotaComparisonQuery(PolicyContract):
    before: HistoricalQuotaQuery
    target: PolicySourceReference
    expected_target_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def two_versions(self) -> "HistoricalQuotaComparisonQuery":
        if self.before.policy.name != self.target.name or self.before.policy == self.target:
            raise ValueError("historical comparison requires two versions of the same policy")
        return self

    def target_query(self) -> HistoricalQuotaQuery:
        return HistoricalQuotaQuery.model_validate(
            {
                **self.before.model_dump(mode="json"),
                "policy": self.target.model_dump(mode="json"),
                "expected_policy_fingerprint": self.expected_target_fingerprint,
            }
        )


class HistoricalQuotaComparison(PolicyContract):
    comparison_only: Literal[True] = True
    historical_only: Literal[True] = True
    current_dependencies_assessed: Literal[False] = False
    role_assignments_verified: Literal[False] = False
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    booking_authorization: Literal[False] = False
    query: HistoricalQuotaComparisonQuery
    before: HistoricalQuotaAssessment
    after: HistoricalQuotaAssessment
    evidence: HistoricalQuotaEvidence
    difference: PolicyDifference
    quota_state_changed: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def one_original(self) -> "HistoricalQuotaComparison":
        HistoricalQuotaReview(assessment=self.before, evidence=self.evidence)
        HistoricalQuotaReview(assessment=self.after, evidence=self.evidence)
        expected = (
            None
            if "not_evaluated" in (self.before.state, self.after.state)
            else self.before.state != self.after.state
        )
        if (
            self.before.query != self.query.before
            or self.after.query != self.query.target_query()
            or self.difference.policy_name != self.query.before.policy.name
            or self.difference.before_fingerprint != self.query.before.expected_policy_fingerprint
            or self.difference.after_fingerprint != self.query.expected_target_fingerprint
            or self.quota_state_changed != expected
        ):
            raise ValueError("historical comparison differs from its single fixed input")
        return self
