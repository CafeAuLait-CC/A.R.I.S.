"""Read one public Lyra evidence value, then compare two exact Nova sources."""

from time import monotonic

from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.quota_evidence_schemas import HistoricalQuotaEvidence
from aris.modules.nova.policy_review import policy_difference
from aris.modules.nova.policy_source import CompiledPolicy
from aris.modules.nova.quota_comparison_schemas import (
    HistoricalQuotaComparison,
    HistoricalQuotaComparisonQuery,
)
from aris.modules.nova.quota_history_service import (
    HistoricalQuotaAssessmentService,
    historical_quota_assessment,
)


def compare_historical_quota(
    before: CompiledPolicy,
    after: CompiledPolicy,
    query: HistoricalQuotaComparisonQuery,
    evidence: HistoricalQuotaEvidence,
) -> HistoricalQuotaComparison:
    query = HistoricalQuotaComparisonQuery.model_validate(query.model_dump(mode="json"))
    first = historical_quota_assessment(before, query.before, evidence)
    second = historical_quota_assessment(after, query.target_query(), evidence)
    return HistoricalQuotaComparison(
        query=query,
        before=first,
        after=second,
        evidence=evidence,
        difference=policy_difference(before, after),
        quota_state_changed=None
        if "not_evaluated" in (first.state, second.state)
        else first.state != second.state,
    )


class HistoricalQuotaComparisonService:
    def __init__(self, assessments: HistoricalQuotaAssessmentService) -> None:
        self.assessments = assessments

    def compare(
        self, actor: ActorContext, query: HistoricalQuotaComparisonQuery
    ) -> HistoricalQuotaComparison:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = HistoricalQuotaComparisonQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 3
        target = query.target_query()
        before = self.assessments._source(actor, query.before)
        after = self.assessments._source(actor, target)
        evidence = self.assessments.usage.read(actor, query.before.usage)
        result = compare_historical_quota(before, after, query, evidence)
        self.assessments.usage.require_access(actor, query.before.usage)
        # Sources are immutable, but the current reader may have lost access meanwhile.
        self.assessments._source(actor, query.before)
        self.assessments._source(actor, target)
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Historical comparison read budget exceeded", 503
            )
        return result
