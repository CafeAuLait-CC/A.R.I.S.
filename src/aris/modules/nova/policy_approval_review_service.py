"""Cross-signer approval metadata, with current Core qualification in batches."""

from time import monotonic
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import (
    AccessRequirement,
    AccessScope,
    AuthorizedUsersQuery,
    UserPrincipal,
)
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.nova.policy_approval_repo import PolicyApprovalRepository, inconsistent_approval
from aris.modules.nova.policy_approval_review_schemas import (
    APPROVAL_REVIEW_LIMIT,
    ApprovalReviewEntry,
    PolicyApprovalReview,
    PolicyApprovalReviewQuery,
    PolicyApprovalReviewRequirements,
)
from aris.modules.nova.policy_approval_schemas import PolicyApprovalSummary, approval_fingerprint
from aris.modules.nova.policy_approval_service import APPROVE_PERMISSION, PolicyApprovalService
from aris.modules.nova.policy_catalog_schemas import PolicySourceView
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, GLOBAL_SCOPE


class PolicyApprovalReviewService:
    def __init__(
        self, approvals: PolicyApprovalService, requirements: PolicyApprovalReviewRequirements
    ) -> None:
        if approvals.requirements != requirements.approval:
            raise ValueError("approval review requires matching individual approval requirements")
        self.approvals, self.catalog, self.sessions = (
            approvals,
            approvals.catalog,
            approvals.sessions,
        )
        self.requirements = PolicyApprovalReviewRequirements.model_validate(
            requirements.model_dump(mode="json")
        )

    def _configured(
        self, query: PolicyApprovalReviewQuery, requirements: PolicyApprovalReviewRequirements
    ) -> None:
        if (
            query.expected_review_requirements_fingerprint != requirements.fingerprint
            or self.requirements != requirements
            or self.approvals.requirements != requirements.approval
            or self.approvals.history.configuration != requirements.approval.history
        ):
            raise DomainError("CONFLICT", "Approval review requirements changed", 409)

    def _subjects(
        self,
        db: Session,
        ids: tuple[UUID, ...],
        nodes: tuple[str, ...],
        requirements: PolicyApprovalReviewRequirements,
    ) -> tuple[UserPrincipal, ...]:
        required = (
            AccessRequirement(permission=APPROVE_PERMISSION, scope=GLOBAL_SCOPE),
            AccessRequirement(permission=DRAFT_PERMISSION, scope=GLOBAL_SCOPE),
            *(
                AccessRequirement(
                    permission="usage.read_own", scope=AccessScope(type="node", id=node)
                )
                for node in nodes
            ),
        )
        return self.catalog.access.permitted_users_in_transaction(
            db,
            AuthorizedUsersQuery(
                user_ids=ids,
                provider=requirements.namespace.provider,
                namespace=requirements.namespace.namespace,
                requirements=required,
            ),
        )

    def _source(
        self,
        db: Session,
        actor: ActorContext,
        query: PolicyApprovalReviewQuery,
        requirements: PolicyApprovalReviewRequirements,
    ) -> PolicySourceView:
        self._configured(query, requirements)
        if not self._subjects(db, (actor.actor_id,), (), requirements):
            raise DomainError(
                "PERMISSION_DENIED", "Approval reviewer is unavailable in this workspace", 403
            )
        source = self.catalog.exact_in_transaction(db, actor, query.source)
        if source.fingerprint != query.expected_source_fingerprint:
            raise DomainError("CONFLICT", "Approval review source fingerprint differs", 409)
        spec = source.source.spec
        if (
            spec.quota.scope != "per_node"
            or len(spec.quota.node_limits) > 8
            or spec.role_overrides
            or spec.idle_reclaim.mode != "disabled"
        ):
            raise DomainError(
                "POLICY_NOT_SUPPORTED",
                "This source is outside the configured approval workflow",
                409,
            )
        return source

    @staticmethod
    def _latest(values: tuple[PolicyApprovalSummary, ...]) -> tuple[PolicyApprovalSummary, ...]:
        seen: set[UUID] = set()
        latest = []
        for item in values:
            if item.approved_by not in seen:
                seen.add(item.approved_by)
                latest.append(item)
        return tuple(latest)

    def review(self, actor: ActorContext, query: PolicyApprovalReviewQuery) -> PolicyApprovalReview:
        query = PolicyApprovalReviewQuery.model_validate(query.model_dump(mode="json"))
        requirements = self.requirements
        started = monotonic()
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                source = self._source(db, actor, query, requirements)
                quota = source.source.spec.quota
                assert quota.scope == "per_node"
                repo = PolicyApprovalRepository(db)
                candidates = repo.review_candidates(source.id)
                if any(
                    (item.source, item.source_fingerprint) != (query.source, source.fingerprint)
                    for item in candidates
                ):
                    raise inconsistent_approval()
                values = candidates[:APPROVAL_REVIEW_LIMIT]
                latest = self._latest(values)
                now = self.catalog._now()
                if any(item.approved_at > now for item in values):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE",
                        "Approval timestamps are ahead of the current clock",
                        503,
                    )
                possible = tuple(
                    item
                    for item in latest
                    if item.revocation_id is None
                    and item.expires_at > now
                    and item.requirements_fingerprint == requirements.approval.fingerprint
                )
                originals = repo.review_originals(source.id, possible)
                for item in possible:
                    value = originals.get(item.id)
                    if value is None or (
                        value.preview.source_id,
                        value.preview.source_author,
                        value.approved_by,
                        value.fingerprint,
                        value.preview.requirements,
                    ) != (
                        source.id,
                        source.created_by,
                        item.approved_by,
                        item.fingerprint,
                        requirements.approval,
                    ):
                        raise inconsistent_approval()
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                self._source(db, actor, query, requirements)
                if PolicyApprovalRepository(db).review_candidates(source.id) != candidates:
                    raise DomainError(
                        "CONFLICT", "Approval statements changed during review; refresh", 409
                    )
                nodes = tuple(sorted(item.node_id for item in quota.node_limits))
                qualified = {
                    item.user_id: item
                    for item in self._subjects(
                        db, tuple(item.approved_by for item in latest), nodes, requirements
                    )
                }
                now = self.catalog._now()
                entries = []
                seen: set[UUID] = set()
                for item in values:
                    state = (
                        "superseded"
                        if item.approved_by in seen
                        else "revoked"
                        if item.revocation_id is not None
                        else "expired"
                        if item.expires_at <= now
                        else "different_requirements"
                        if item.requirements_fingerprint != requirements.approval.fingerprint
                        else "approver_unavailable"
                        if item.approved_by not in qualified
                        else "counted"
                    )
                    if state == "counted" and item.id not in originals:
                        raise DomainError(
                            "CONFLICT", "Approval timing changed during review; refresh", 409
                        )
                    seen.add(item.approved_by)
                    principal = qualified.get(item.approved_by)
                    entries.append(
                        ApprovalReviewEntry(
                            approval=item,
                            approver_label=principal.display_name
                            if principal is not None
                            else None,
                            state=state,
                        )
                    )
                # Current requester access is separate from the people being counted.
                if not self._subjects(db, (actor.actor_id,), (), requirements):
                    raise DomainError("PERMISSION_DENIED", "Approval reviewer access changed", 403)
            self._configured(query, requirements)
            if monotonic() - started >= 3:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Approval review read budget exceeded", 503
                )
            count = sum(item.state == "counted" for item in entries)
            truncated = len(candidates) > APPROVAL_REVIEW_LIMIT
            draft = PolicyApprovalReview.model_construct(
                query=query,
                source_id=source.id,
                requirements=requirements,
                as_of=now,
                truncated=truncated,
                entries=tuple(entries),
                qualifying_signers=count,
                outcome="minimum_met"
                if count >= requirements.minimum_distinct_approvers
                else "unknown"
                if truncated
                else "minimum_not_met",
                fingerprint="0" * 64,
            )
            return PolicyApprovalReview.model_validate(
                {
                    **draft.model_dump(mode="json"),
                    "fingerprint": approval_fingerprint(
                        "review_result", draft, exclude={"fingerprint", "as_of"}
                    ),
                }
            )
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Approval review database is unavailable",
                503,
            ) from None
