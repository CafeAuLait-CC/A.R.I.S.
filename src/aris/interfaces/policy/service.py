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
from aris.interfaces.policy.history import (
    PolicyHistoricalUsage,
    PolicyHistoryAssessmentInput,
    PolicyHistoryAssessmentView,
    PolicyHistoryWindowsInput,
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
            ),
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
