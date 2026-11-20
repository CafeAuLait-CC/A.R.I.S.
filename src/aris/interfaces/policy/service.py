"""Read-only adapter for stored policy sources, not a policy publication surface."""

from collections.abc import Callable
from time import monotonic
from typing import TypeVar

from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract, AccessRequest, UserPrincipal
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.commands.service import bounded_json
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import BoundSubjectTokens
from aris.interfaces.policy.drafts import (
    PolicyDraftCommandSurface,
    PolicyDraftInput,
    PolicyDraftValidation,
)
from aris.interfaces.policy.feedback import PolicyFeedbackValidation
from aris.interfaces.policy.history import (
    PolicyHistoricalUsage,
    PolicyHistoryAssessmentInput,
    PolicyHistoryAssessmentView,
    PolicyHistoryRecordInput,
    PolicyHistoryRecordValidation,
    PolicyHistoryReviewInput,
    PolicyHistoryReviewView,
    PolicyHistoryWindowsInput,
    PolicyStoredHistoryView,
)
from aris.interfaces.policy.history_comparison import (
    PolicyHistoryComparison,
    PolicyHistoryComparisonExport,
    PolicyHistoryComparisonInput,
    PolicyHistoryComparisonView,
)
from aris.interfaces.policy.materials import (
    PolicyExampleResultView,
    PolicyMaterialComparedCase,
    PolicyMaterialComparisonInput,
    PolicyMaterialComparisonView,
    PolicyMaterialDetailInput,
    PolicyMaterialDetailView,
    PolicyMaterialTextInput,
    PolicyMaterialValidation,
)
from aris.modules.lyra.quota_evidence_schemas import QuotaEvidencePage
from aris.modules.nova.policy_approval_review_schemas import (
    PolicyApprovalReview,
    PolicyApprovalReviewExport,
    PolicyApprovalReviewQuery,
)
from aris.modules.nova.policy_approval_review_service import PolicyApprovalReviewService
from aris.modules.nova.policy_approval_revocation_service import PolicyApprovalRevocationService
from aris.modules.nova.policy_approval_schemas import (
    ApprovePolicySource,
    PolicyApprovalDetailQuery,
    PolicyApprovalListQuery,
    PolicyApprovalPage,
    PolicyApprovalPreview,
    PolicyApprovalRequirements,
    PolicyApprovalRevocationPreview,
    PolicyApprovalStatus,
    RevokePolicySourceApproval,
)
from aris.modules.nova.policy_approval_service import APPROVE_PERMISSION, PolicyApprovalService
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
    PolicySourcePage,
    PolicySourceReference,
    PolicySourceView,
    RecordPolicyDraft,
    StoredPolicyComparison,
)
from aris.modules.nova.policy_catalog_service import (
    DRAFT_PERMISSION,
    GLOBAL_SCOPE,
    PolicyCatalogService,
)
from aris.modules.nova.policy_feedback_schemas import (
    PolicyFeedbackListQuery,
    PolicyFeedbackPage,
    RecordPolicyFeedback,
)
from aris.modules.nova.policy_feedback_service import PolicyFeedbackService
from aris.modules.nova.policy_history_record_schemas import (
    HistoricalReviewDetailQuery,
    HistoricalReviewListQuery,
    HistoricalReviewPage,
)
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService
from aris.modules.nova.policy_material_schemas import (
    PolicyMaterialListQuery,
    PolicyMaterialPage,
    RecordPolicyReviewMaterial,
)
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from aris.modules.nova.policy_review import policy_difference
from aris.modules.nova.policy_schemas import PolicyName
from aris.modules.nova.policy_simulation import simulate_policy_constraints
from aris.modules.nova.policy_source import compile_policy, parse_policy_source

Result = TypeVar("Result", bound=BaseModel)


class PolicyCatalogSession(AccessContract):
    actor: UserPrincipal
    policy_names: tuple[PolicyName, ...] = Field(min_length=1, max_length=32)
    draft_editing_available: bool = False
    historical_usage_nodes: tuple[str, ...] = ()
    review_materials_available: bool = False
    review_material_implementation_ref: str | None = None
    material_comparison_implementation_ref: str | None = None
    feedback_available: bool = False
    feedback_writing_available: bool = False
    historical_review_implementation_ref: str | None = None
    historical_review_records_available: bool = False
    historical_review_writing_available: bool = False
    historical_comparison_available: bool = False
    source_approval_requirements: PolicyApprovalRequirements | None = None
    source_approval_requirements_fingerprint: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    source_approval_writing_available: bool = False
    source_approval_review_available: bool = False
    source_approval_review_requirements_fingerprint: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    source_approval_review_minimum_distinct_approvers: int | None = Field(default=None, ge=1, le=16)


class PolicyDetailInput(AccessContract):
    reference: PolicySourceReference
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyReadSurface:
    def __init__(
        self,
        catalog: PolicyCatalogService,
        authenticator: BoundSubjectTokens,
        policy_names: tuple[str, ...],
        *,
        draft_commands: PolicyDraftCommandSurface | None = None,
        historical_usage: PolicyHistoricalUsage | None = None,
        review_materials: PolicyReviewMaterialService | None = None,
        material_comparison_implementation_ref: str | None = None,
        feedback: PolicyFeedbackService | None = None,
        historical_review_implementation_ref: str | None = None,
        historical_reviews: PolicyHistoricalReviewService | None = None,
        historical_comparison_enabled: bool = False,
        approvals: PolicyApprovalService | None = None,
        approval_summary: PolicyApprovalReviewService | None = None,
    ) -> None:
        if not 1 <= len(policy_names) <= 32 or len(set(policy_names)) != len(policy_names):
            raise ValueError("policy interface requires explicit unique policy names")
        for name in policy_names:
            PolicySourceReference(name=name, version=1)
        if draft_commands is not None and (
            draft_commands.authentication is not authenticator
            or draft_commands.policy_names != policy_names
        ):
            raise ValueError("policy reads and draft commands require one user and name boundary")
        self.catalog, self.authenticator, self.policy_names = catalog, authenticator, policy_names
        self.draft_commands = draft_commands
        if historical_usage is not None and historical_usage.assessments.catalog is not catalog:
            raise ValueError("historical policy reads require the same source catalog")
        self.historical_usage = historical_usage
        if review_materials is not None and review_materials.catalog is not catalog:
            raise ValueError("policy material reads require the same source catalog")
        self.review_materials = review_materials
        if material_comparison_implementation_ref is not None:
            from pydantic import TypeAdapter

            from aris.modules.nova.policy_material_schemas import ImplementationReference

            material_comparison_implementation_ref = TypeAdapter(
                ImplementationReference
            ).validate_python(material_comparison_implementation_ref)
            if review_materials is None:
                raise ValueError("stored material comparisons require material reads")
        self.material_comparison_implementation_ref = material_comparison_implementation_ref
        if feedback is not None and feedback.catalog is not catalog:
            raise ValueError("feedback reads require the same source catalog")
        if draft_commands is not None and draft_commands.feedback_writing and feedback is None:
            raise ValueError("feedback writing requires its explicit read interface")
        self.feedback = feedback
        if historical_review_implementation_ref is not None:
            from pydantic import TypeAdapter

            from aris.modules.nova.policy_material_schemas import ImplementationReference

            historical_review_implementation_ref = TypeAdapter(
                ImplementationReference
            ).validate_python(historical_review_implementation_ref)
            if historical_usage is None:
                raise ValueError("historical review requires explicit historical usage methods")
        self.historical_review_implementation_ref = historical_review_implementation_ref
        if historical_comparison_enabled and (
            historical_usage is None or historical_review_implementation_ref is None
        ):
            raise ValueError("historical comparison requires configured history and implementation")
        self.historical_comparison = (
            PolicyHistoryComparison(historical_usage, historical_review_implementation_ref)
            if historical_comparison_enabled
            and historical_usage is not None
            and historical_review_implementation_ref is not None
            else None
        )
        if historical_reviews is not None and (
            historical_reviews.catalog is not catalog
            or historical_usage is None
            or historical_review_implementation_ref
            != historical_reviews.configuration.implementation_ref
            or historical_usage.nodes != historical_reviews.configuration.nodes
        ):
            raise ValueError(
                "historical record reads require the same catalog and configured methods"
            )
        if (
            draft_commands is not None
            and draft_commands.historical_review_storage is not None
            and (
                historical_reviews is None
                or not historical_reviews.writing_enabled
                or historical_reviews.configuration != draft_commands.historical_review_storage
            )
        ):
            raise ValueError("historical record writing requires its matching explicit reader")
        self.historical_reviews = historical_reviews
        if approvals is not None and (
            approvals.catalog is not catalog or approvals.history is not historical_reviews
        ):
            raise ValueError("source approval requires its matching historical reader")
        if (
            draft_commands is not None
            and draft_commands.source_approval is not None
            and (
                approvals is None
                or not approvals.writing_enabled
                or approvals.requirements != draft_commands.source_approval
            )
        ):
            raise ValueError(
                "source approval writing requires its matching reader and requirements"
            )
        self.approvals = approvals
        if approval_summary is not None and (
            approval_summary.approvals is not approvals
            or any(
                (subject.provider, subject.namespace)
                != (
                    approval_summary.requirements.namespace.provider,
                    approval_summary.requirements.namespace.namespace,
                )
                for _, subject in authenticator.bindings
            )
        ):
            raise ValueError(
                "approval review requires the same approval reader and configured workspace"
            )
        self.approval_summary = approval_summary

    def _approval_requirements(
        self, session: Session, actor: ActorContext
    ) -> PolicyApprovalRequirements | None:
        if self.approvals is None:
            return None
        try:
            self.catalog.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=actor.actor_id, permission=APPROVE_PERMISSION, scope=GLOBAL_SCOPE
                ),
            )
        except DomainError as error:
            if error.code != "PERMISSION_DENIED":
                raise
            return None
        return self.approvals.requirements

    def approval_validate(
        self, authorization: str | None, request_id: str, query: ApprovePolicySource
    ) -> PolicyApprovalPreview:
        approvals = self.approvals
        if (
            approvals is None
            or self.draft_commands is None
            or self.draft_commands.source_approval is None
        ):
            raise DomainError(
                "POLICY_APPROVAL_NOT_CONFIGURED", "Source approval writing is not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: approvals.preview(actor, query),
            names=(query.source.name,),
        )

    def approval_records(
        self, authorization: str | None, request_id: str, query: PolicyApprovalListQuery
    ) -> PolicyApprovalPage:
        approvals = self.approvals
        if approvals is None:
            raise DomainError(
                "POLICY_APPROVAL_NOT_CONFIGURED", "Source approval reads are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: approvals.list(actor, query),
            names=(query.source.name,),
        )

    def approval_record(
        self, authorization: str | None, request_id: str, query: PolicyApprovalDetailQuery
    ) -> PolicyApprovalStatus:
        approvals = self.approvals
        if approvals is None:
            raise DomainError(
                "POLICY_APPROVAL_NOT_CONFIGURED", "Source approval reads are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: approvals.status(actor, query),
            names=(query.source.name,),
        )

    def approval_revoke_validate(
        self, authorization: str | None, request_id: str, query: RevokePolicySourceApproval
    ) -> PolicyApprovalRevocationPreview:
        approvals = self.approvals
        if (
            approvals is None
            or self.draft_commands is None
            or self.draft_commands.source_approval is None
        ):
            raise DomainError(
                "POLICY_APPROVAL_NOT_CONFIGURED", "Source approval writing is not enabled", 503
            )
        revocations = PolicyApprovalRevocationService(approvals)
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: revocations.preview(actor, query),
            names=(query.source.name,),
        )

    def approval_review_read(
        self, authorization: str | None, request_id: str, query: PolicyApprovalReviewQuery
    ) -> PolicyApprovalReview:
        service = self.approval_summary
        if service is None:
            raise DomainError(
                "POLICY_APPROVAL_REVIEW_NOT_CONFIGURED", "Approval review is not configured", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: service.review(actor, query),
            names=(query.source.name,),
        )

    def approval_review_export(
        self, authorization: str | None, request_id: str, query: PolicyApprovalReviewExport
    ) -> PolicyApprovalReview:
        original = PolicyApprovalReviewQuery.model_validate(
            query.model_dump(mode="json", exclude={"expected_result_fingerprint"})
        )
        value = self.approval_review_read(authorization, request_id, original)
        if value.fingerprint != query.expected_result_fingerprint:
            raise DomainError("CONFLICT", "Approval review changed; refresh before exporting", 409)
        return value

    def _run(
        self,
        authorization: str | None,
        request_id: str,
        operation: Callable[[Session, ActorContext, UserPrincipal], Result],
        *,
        names: tuple[str, ...] = (),
    ) -> Result:
        subject = self.authenticator.authenticate(authorization)
        deadline = monotonic() + 3
        try:
            with self.catalog.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                principal = self.catalog.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
                actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
                access = AccessRequest(
                    user_id=actor.actor_id, permission=DRAFT_PERMISSION, scope=GLOBAL_SCOPE
                )
                self.catalog.access.require_in_transaction(session, access)
                for name in names:
                    self._name(name)
                result = operation(session, actor, principal)
                self.catalog.access.require_in_transaction(session, access)
                current = self.catalog.access.resolve_external_in_transaction(session, subject)
                if current is None or current.user_id != principal.user_id:
                    raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Policy read budget exceeded", 503
                    )
                return result
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Policy database is unavailable", 503
            ) from None

    def session(self, authorization: str | None, request_id: str) -> PolicyCatalogSession:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: PolicyCatalogSession(
                actor=principal,
                policy_names=self.policy_names,
                draft_editing_available=self.draft_commands is not None,
                historical_usage_nodes=self.historical_usage.accessible_nodes(session, actor)
                if self.historical_usage is not None
                else (),
                review_materials_available=self.review_materials is not None,
                review_material_implementation_ref=self.draft_commands.material_implementation_ref
                if self.draft_commands is not None and self.review_materials is not None
                else None,
                material_comparison_implementation_ref=self.material_comparison_implementation_ref,
                feedback_available=self.feedback is not None,
                feedback_writing_available=self.feedback is not None
                and self.draft_commands is not None
                and self.draft_commands.feedback_writing,
                historical_review_implementation_ref=self.historical_review_implementation_ref,
                historical_comparison_available=self.historical_comparison is not None,
                historical_review_records_available=self.historical_reviews is not None,
                historical_review_writing_available=self.historical_reviews is not None
                and self.draft_commands is not None
                and self.draft_commands.historical_review_storage is not None,
                source_approval_requirements=(
                    requirements := self._approval_requirements(session, actor)
                ),
                source_approval_requirements_fingerprint=requirements.fingerprint
                if requirements is not None
                else None,
                source_approval_writing_available=requirements is not None
                and self.draft_commands is not None
                and self.draft_commands.source_approval is not None,
                source_approval_review_available=requirements is not None
                and self.approval_summary is not None,
                source_approval_review_requirements_fingerprint=self.approval_summary.requirements.fingerprint
                if requirements is not None and self.approval_summary is not None
                else None,
                source_approval_review_minimum_distinct_approvers=self.approval_summary.requirements.minimum_distinct_approvers
                if requirements is not None and self.approval_summary is not None
                else None,
            ),
        )

    def feedback_validate(
        self, authorization: str | None, request_id: str, query: RecordPolicyFeedback
    ) -> PolicyFeedbackValidation:
        feedback = self.feedback
        if (
            feedback is None
            or self.draft_commands is None
            or not self.draft_commands.feedback_writing
        ):
            raise DomainError(
                "POLICY_FEEDBACK_NOT_CONFIGURED", "Feedback writing is not enabled", 503
            )

        def validate(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyFeedbackValidation:
            feedback.validate_in_transaction(session, actor, query)
            return PolicyFeedbackValidation(
                request=query, inline_bytes=len(bounded_json(query.model_dump(mode="json"), 12288))
            )

        return self._run(authorization, request_id, validate, names=(query.source.name,))

    def feedback_list(
        self, authorization: str | None, request_id: str, query: PolicyFeedbackListQuery
    ) -> PolicyFeedbackPage:
        feedback = self.feedback
        if feedback is None:
            raise DomainError(
                "POLICY_FEEDBACK_NOT_CONFIGURED", "Feedback reads are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: feedback.list_in_transaction(session, actor, query),
            names=(query.source.name,),
        )

    def materials_validate(
        self, authorization: str | None, request_id: str, query: RecordPolicyReviewMaterial
    ) -> PolicyMaterialValidation:
        configured = (
            self.draft_commands.material_implementation_ref if self.draft_commands else None
        )
        if configured is None:
            raise DomainError(
                "POLICY_REVIEW_NOT_CONFIGURED", "Material writing is not enabled", 503
            )

        def validate(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyMaterialValidation:
            if query.expected_implementation_ref != configured:
                raise DomainError("CONFLICT", "Material implementation reference changed", 409)
            source = self.catalog.exact_in_transaction(session, actor, query.source)
            if source.fingerprint != query.expected_source_fingerprint:
                raise DomainError("CONFLICT", "Material source fingerprint differs", 409)
            size = len(bounded_json(query.model_dump(mode="json"), 16384))
            compiled = compile_policy(source.source)
            return PolicyMaterialValidation(
                request=query,
                inline_bytes=size,
                results=tuple(
                    PolicyExampleResultView.of(simulate_policy_constraints(compiled, case))
                    for case in query.scenarios
                ),
            )

        return self._run(authorization, request_id, validate, names=(query.source.name,))

    def materials_validate_text(
        self, authorization: str | None, request_id: str, query: PolicyMaterialTextInput
    ) -> PolicyMaterialValidation:
        return self.materials_validate(authorization, request_id, query.parsed())

    def materials_list(
        self, authorization: str | None, request_id: str, query: PolicyMaterialListQuery
    ) -> PolicyMaterialPage:
        materials = self.review_materials
        if materials is None:
            raise DomainError("POLICY_REVIEW_NOT_CONFIGURED", "Material reads are not enabled", 503)
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: materials.list(actor, query),
            names=(query.source.name,),
        )

    def materials_detail(
        self, authorization: str | None, request_id: str, query: PolicyMaterialDetailInput
    ) -> PolicyMaterialDetailView:
        materials = self.review_materials
        if materials is None:
            raise DomainError("POLICY_REVIEW_NOT_CONFIGURED", "Material reads are not enabled", 503)

        def read(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyMaterialDetailView:
            source = self.catalog.exact_in_transaction(session, actor, query.source)
            if source.fingerprint != query.expected_source_fingerprint:
                raise DomainError("CONFLICT", "Selected policy source fingerprint differs", 409)
            value = materials.read(actor, query.id, expected_source=query.source)
            if (value.source_id, value.request.expected_source_fingerprint, value.fingerprint) != (
                source.id,
                source.fingerprint,
                query.expected_fingerprint,
            ):
                raise DomainError("CONFLICT", "Selected policy review material differs", 409)
            return PolicyMaterialDetailView.of(value)

        return self._run(authorization, request_id, read, names=(query.source.name,))

    def materials_compare(
        self, authorization: str | None, request_id: str, query: PolicyMaterialComparisonInput
    ) -> PolicyMaterialComparisonView:
        materials = self.review_materials
        implementation = self.material_comparison_implementation_ref
        if materials is None or implementation is None:
            raise DomainError(
                "POLICY_REVIEW_NOT_CONFIGURED", "Material comparison is not enabled", 503
            )

        def compare(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyMaterialComparisonView:
            if query.expected_implementation_ref != implementation:
                raise DomainError(
                    "CONFLICT", "Material comparison implementation reference changed", 409
                )
            source = self.catalog.exact_in_transaction(session, actor, query.source)
            if source.fingerprint != query.expected_source_fingerprint:
                raise DomainError("CONFLICT", "Material source fingerprint differs", 409)
            value = materials.read(actor, query.id, expected_source=query.source)
            if (value.source_id, value.request.expected_source_fingerprint, value.fingerprint) != (
                source.id,
                source.fingerprint,
                query.expected_fingerprint,
            ):
                raise DomainError("CONFLICT", "Selected policy review material differs", 409)
            result = self.catalog.compare_sources_in_transaction(
                session,
                actor,
                PolicySourceComparisonQuery(
                    before=query.source,
                    after=query.target,
                    expected_before_fingerprint=query.expected_source_fingerprint,
                    expected_after_fingerprint=query.expected_target_fingerprint,
                    scenarios=value.request.scenarios,
                ),
            )
            if self.material_comparison_implementation_ref != implementation:
                raise DomainError(
                    "CONFLICT", "Material comparison implementation reference changed", 409
                )
            return PolicyMaterialComparisonView(
                material=PolicyMaterialDetailView.of(value).summary,
                comparison_implementation_ref=implementation,
                target=query.target,
                target_source_id=result.after_source_id,
                target_source_fingerprint=query.expected_target_fingerprint,
                difference=result.difference,
                cases=tuple(
                    PolicyMaterialComparedCase(
                        scenario=scenario,
                        raw_scenario=scenario.model_dump_json(indent=2),
                        recorded_result=PolicyExampleResultView.of(original),
                        before_result=PolicyExampleResultView.of(pair.before),
                        after_result=PolicyExampleResultView.of(pair.after),
                        stored_replay_matches=original == pair.before,
                        decision_changed=pair.decision_changed,
                        changed_rules=pair.changed_rules,
                    )
                    for scenario, original, pair in zip(
                        value.request.scenarios, value.results, result.cases, strict=True
                    )
                ),
            )

        return self._run(
            authorization, request_id, compare, names=(query.source.name, query.target.name)
        )

    def history_windows(
        self, authorization: str | None, request_id: str, query: PolicyHistoryWindowsInput
    ) -> QuotaEvidencePage:
        history = self.historical_usage
        if history is None:
            raise DomainError(
                "HISTORICAL_USAGE_NOT_CONFIGURED", "Historical policy reads are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: history.windows(actor, query),
        )

    def history_assess(
        self, authorization: str | None, request_id: str, query: PolicyHistoryAssessmentInput
    ) -> PolicyHistoryAssessmentView:
        history = self.historical_usage
        if history is None:
            raise DomainError(
                "HISTORICAL_USAGE_NOT_CONFIGURED", "Historical policy reads are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: history.assess(actor, query),
            names=(query.reference.name,),
        )

    def history_review(
        self, authorization: str | None, request_id: str, query: PolicyHistoryReviewInput
    ) -> PolicyHistoryReviewView:
        history, reference = self.historical_usage, self.historical_review_implementation_ref
        if history is None or reference is None:
            raise DomainError(
                "HISTORICAL_REVIEW_NOT_CONFIGURED", "Historical review is not configured", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: history.review(actor, query, reference),
            names=(query.reference.name,),
        )

    def history_compare(
        self, authorization: str | None, request_id: str, query: PolicyHistoryComparisonInput
    ) -> PolicyHistoryComparisonView:
        comparison = self.historical_comparison
        if comparison is None:
            raise DomainError(
                "HISTORICAL_COMPARISON_NOT_CONFIGURED",
                "Historical comparison is not configured",
                503,
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: comparison.compare(actor, query),
            names=(query.reference.name, query.target.name),
        )

    def history_compare_export(
        self, authorization: str | None, request_id: str, query: PolicyHistoryComparisonExport
    ) -> PolicyHistoryComparisonView:
        original = PolicyHistoryComparisonInput.model_validate(
            query.model_dump(mode="json", exclude={"expected_context_fingerprint"})
        )
        value = self.history_compare(authorization, request_id, original)
        if value.context_fingerprint != query.expected_context_fingerprint:
            raise DomainError(
                "CONFLICT", "Historical comparison changed; refresh before export", 409
            )
        return value

    def history_record_validate(
        self, authorization: str | None, request_id: str, query: PolicyHistoryRecordInput
    ) -> PolicyHistoryRecordValidation:
        store, history = self.historical_reviews, self.historical_usage
        if (
            store is None
            or history is None
            or self.draft_commands is None
            or self.draft_commands.historical_review_storage is None
        ):
            raise DomainError(
                "POLICY_HISTORY_NOT_CONFIGURED", "Historical review saving is not enabled", 503
            )

        def validate(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyHistoryRecordValidation:
            request = history.record_request(actor, query)
            value = store.preview(actor, request)
            return PolicyHistoryRecordValidation(
                request=request,
                context=PolicyHistoryReviewView.of(value, store.configuration.implementation_ref),
            )

        return self._run(authorization, request_id, validate, names=(query.reference.name,))

    def history_records(
        self, authorization: str | None, request_id: str, query: HistoricalReviewListQuery
    ) -> HistoricalReviewPage:
        store = self.historical_reviews
        if store is None:
            raise DomainError(
                "POLICY_HISTORY_NOT_CONFIGURED", "Historical review records are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: store.list(actor, query),
            names=(query.source.name,),
        )

    def history_record(
        self, authorization: str | None, request_id: str, query: HistoricalReviewDetailQuery
    ) -> PolicyStoredHistoryView:
        store = self.historical_reviews
        if store is None:
            raise DomainError(
                "POLICY_HISTORY_NOT_CONFIGURED", "Historical review records are not enabled", 503
            )
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: PolicyStoredHistoryView.of(store.get(actor, query)),
            names=(query.source.name,),
        )

    def _name(self, name: str) -> None:
        if name not in self.policy_names:
            raise DomainError("PERMISSION_DENIED", "Policy name is outside this interface", 403)

    def sources(
        self, authorization: str | None, request_id: str, query: PolicySourceListQuery
    ) -> PolicySourcePage:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: self.catalog.sources_in_transaction(
                session, actor, query
            ),
            names=(query.name,),
        )

    def detail(
        self, authorization: str | None, request_id: str, query: PolicyDetailInput
    ) -> PolicySourceView:
        def read(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicySourceView:
            view = self.catalog.exact_in_transaction(session, actor, query.reference)
            if view.fingerprint != query.expected_fingerprint:
                raise DomainError("CONFLICT", "Stored policy source fingerprint differs", 409)
            return view

        return self._run(authorization, request_id, read, names=(query.reference.name,))

    def compare(
        self, authorization: str | None, request_id: str, query: PolicySourceComparisonQuery
    ) -> StoredPolicyComparison:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: self.catalog.compare_sources_in_transaction(
                session, actor, query
            ),
            names=(query.before.name, query.after.name),
        )

    def validate_draft(
        self, authorization: str | None, request_id: str, query: PolicyDraftInput
    ) -> PolicyDraftValidation:
        if self.draft_commands is None:
            raise DomainError("POLICY_DRAFTS_NOT_CONFIGURED", "Draft editing is not enabled", 503)

        def validate(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicyDraftValidation:
            try:
                source = parse_policy_source(query.source_text.encode("utf-8"))
                compiled = compile_policy(source)
                draft = RecordPolicyDraft(source=compiled.source, reason=query.reason)
            except ValueError:
                raise DomainError("VALIDATION_ERROR", "Invalid policy draft", 422) from None
            self._name(compiled.source.metadata.name)
            inline_bytes = len(bounded_json(draft.model_dump(mode="json"), 16384))
            reference = PolicySourceReference(
                name=compiled.source.metadata.name, version=compiled.source.metadata.version
            )
            existing = None
            try:
                existing = self.catalog.exact_in_transaction(session, actor, reference)
            except DomainError as error:
                if error.code != "NOT_FOUND":
                    raise
            if existing is not None and existing.fingerprint != compiled.fingerprint:
                raise DomainError("CONFLICT", "This version already contains different rules", 409)
            difference = None
            if query.base is not None:
                self._name(query.base.name)
                if query.base.name != reference.name:
                    raise DomainError("VALIDATION_ERROR", "Draft and base policy names differ", 422)
                base = self.catalog.exact_in_transaction(session, actor, query.base)
                if base.fingerprint != query.expected_base_fingerprint:
                    raise DomainError("CONFLICT", "Base source fingerprint differs", 409)
                difference = policy_difference(compile_policy(base.source), compiled)
            return PolicyDraftValidation(
                draft=draft,
                fingerprint=compiled.fingerprint,
                inline_bytes=inline_bytes,
                difference=difference,
                existing_equivalent=existing is not None,
            )

        return self._run(authorization, request_id, validate)
