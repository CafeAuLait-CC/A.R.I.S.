"""Consume Lyra's public historical evidence without reading its storage internals."""

from fractions import Fraction
from typing import Protocol

from sqlalchemy.exc import DBAPIError

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.quota_evidence_schemas import HistoricalQuotaEvidence, QuotaEvidenceSelection
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from aris.modules.nova.policy_schemas import NodeQuotaRules
from aris.modules.nova.policy_simulation import ExactGpuSeconds
from aris.modules.nova.policy_source import CompiledPolicy, compile_policy
from aris.modules.nova.policy_units import policy_quota_window, weighted_gpu_seconds
from aris.modules.nova.quota_history_schemas import HistoricalQuotaAssessment, HistoricalQuotaQuery


class HistoricalUsageReader(Protocol):
    def read(
        self, actor: ActorContext, selection: QuotaEvidenceSelection
    ) -> HistoricalQuotaEvidence: ...

    def require_access(self, actor: ActorContext, selection: QuotaEvidenceSelection) -> None: ...


def exact_seconds(value: Fraction) -> ExactGpuSeconds:
    return ExactGpuSeconds(numerator=value.numerator, denominator=value.denominator)


def historical_quota_assessment(
    compiled: CompiledPolicy, query: HistoricalQuotaQuery, evidence: HistoricalQuotaEvidence
) -> HistoricalQuotaAssessment:
    """Pure historical calculation; the service owns source and evidence authority."""
    query = HistoricalQuotaQuery.model_validate(query.model_dump(mode="json"))
    evidence = HistoricalQuotaEvidence.model_validate(evidence.model_dump(mode="json"))
    canonical = compile_policy(compiled.source)
    if (
        canonical != compiled
        or compiled.fingerprint != query.expected_policy_fingerprint
        or (compiled.source.metadata.name, compiled.source.metadata.version)
        != (query.policy.name, query.policy.version)
        or evidence.selection != query.usage
    ):
        raise DomainError("CONFLICT", "Historical policy or evidence binding differs", 409)
    spec = compiled.source.spec
    period = policy_quota_window(spec, query.period_at)
    selected = next(
        (item for item in spec.role_overrides if item.role in query.assumed_roles), None
    )
    limit = None
    if isinstance(spec.quota, NodeQuotaRules):
        limit = next(
            (item.limit for item in spec.quota.node_limits if item.node_id == query.usage.node_id),
            None,
        )
        if limit is not None and selected and selected.limits.quota_limit is not None:
            limit = selected.limits.quota_limit
    reason = None
    if not isinstance(spec.quota, NodeQuotaRules):
        reason = "GLOBAL_QUOTA_REQUIRES_ALL_NODE_EVIDENCE"
    elif limit is None:
        reason = "QUOTA_SCOPE_UNCONFIGURED"
    elif (evidence.configuration_fingerprint, evidence.timing_fingerprint) != (
        query.expected_configuration_fingerprint,
        query.expected_timing_fingerprint,
    ):
        reason = "USAGE_METHOD_MISMATCH"
    elif (
        evidence.window.timezone != spec.timezone
        or evidence.window.start_at != period.start_at
        or evidence.window.end_at > period.end_at
        or (spec.quota.window.kind == "rolling_7d" and evidence.window.end_at != period.end_at)
    ):
        reason = "USAGE_WINDOW_MISMATCH"
    elif evidence.coverage != "complete":
        reason = "USAGE_COVERAGE_INCOMPLETE"
    used = remaining = None
    state = "not_evaluated"
    if reason is None:
        assert limit is not None and evidence.models is not None
        # Aggregation can exceed one GPU-year; the rate is exact per microsecond.
        weighted = sum(
            (
                weighted_gpu_seconds(1, item.model, spec.gpu_time_weights)
                * item.observed_microseconds
                for item in evidence.models
            ),
            Fraction(),
        )
        used = exact_seconds(weighted)
        if limit.kind == "unlimited":
            state, reason = "unlimited", "QUOTA_UNLIMITED"
        else:
            accepted = weighted < limit.gpu_seconds
            state = "within_limit" if accepted else "exhausted"
            reason = (
                "WITHIN_RECORDED_HISTORICAL_LIMIT" if accepted else "HISTORICAL_QUOTA_EXHAUSTED"
            )
            remaining = exact_seconds(max(Fraction(), Fraction(limit.gpu_seconds) - weighted))
    result = HistoricalQuotaAssessment(
        query=query,
        node_id=query.usage.node_id,
        owner_id=query.usage.owner_id,
        period=period,
        sampled_until=evidence.window.end_at,
        captured_at=evidence.captured_at,
        snapshot_created_at=evidence.created_at,
        evidence_fingerprint=evidence.fingerprint,
        state=state,
        reason=reason,
        selected_assumed_role=selected.role if selected else None,
        limit=limit,
        used_gpu_seconds=used,
        remaining_gpu_seconds=remaining,
    )
    return result


class HistoricalQuotaAssessmentService:
    def __init__(self, catalog: PolicyCatalogService, usage: HistoricalUsageReader) -> None:
        self.catalog, self.usage = catalog, usage

    def _source(self, actor: ActorContext, query: HistoricalQuotaQuery) -> CompiledPolicy:
        try:
            with self.catalog.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                source = self.catalog.exact_in_transaction(session, actor, query.policy)
                if source.fingerprint != query.expected_policy_fingerprint:
                    raise DomainError(
                        "CONFLICT", "Historical quota policy fingerprint differs", 409
                    )
                return compile_policy(source.source)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Historical quota policy database is unavailable",
                503,
            ) from None

    def assess(self, actor: ActorContext, query: HistoricalQuotaQuery) -> HistoricalQuotaAssessment:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = HistoricalQuotaQuery.model_validate(query.model_dump(mode="json"))
        compiled = self._source(actor, query)
        evidence = self.usage.read(actor, query.usage)
        result = historical_quota_assessment(compiled, query, evidence)
        # Each domain rechecks current permissions after the combined calculation.
        self.usage.require_access(actor, query.usage)
        self._source(actor, query)
        return result
