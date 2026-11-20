"""Read one public Lyra evidence value, then compare two exact Nova sources."""

from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.quota_evidence_schemas import HistoricalQuotaEvidence
from aris.modules.nova.policy_review import policy_difference
from aris.modules.nova.policy_source import CompiledPolicy, compile_policy
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

    def _sources(
        self, actor: ActorContext, query: HistoricalQuotaComparisonQuery
    ) -> tuple[CompiledPolicy, CompiledPolicy]:
        catalog = self.assessments.catalog
        try:
            with catalog.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                before = catalog.exact_in_transaction(db, actor, query.before.policy)
                after = catalog.exact_in_transaction(db, actor, query.target)
                if (before.fingerprint, after.fingerprint) != (
                    query.before.expected_policy_fingerprint,
                    query.expected_target_fingerprint,
                ):
                    raise DomainError("CONFLICT", "Historical comparison source differs", 409)
                return compile_policy(before.source), compile_policy(after.source)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Historical comparison sources are unavailable",
                503,
            ) from None

    def compare(
        self, actor: ActorContext, query: HistoricalQuotaComparisonQuery
    ) -> HistoricalQuotaComparison:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = HistoricalQuotaComparisonQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 3
        before, after = self._sources(actor, query)
        evidence = self.assessments.usage.read(actor, query.before.usage)
        result = compare_historical_quota(before, after, query, evidence)
        self.assessments.usage.require_access(actor, query.before.usage)
        # Sources are immutable, but the current reader may have lost access meanwhile.
        self._sources(actor, query)
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Historical comparison read budget exceeded", 503
            )
        return result
