"""Explicit human source approval; never supplies publication authority."""

from datetime import timedelta
from time import monotonic
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.policy_approval_repo import PolicyApprovalRepository
from aris.modules.nova.policy_approval_revocation_repo import PolicyApprovalRevocationRepository
from aris.modules.nova.policy_approval_schemas import (
    ApprovedHistoricalSample,
    ApprovePolicySource,
    PolicyApprovalCursor,
    PolicyApprovalDetailQuery,
    PolicyApprovalListQuery,
    PolicyApprovalPage,
    PolicyApprovalPreview,
    PolicyApprovalReceipt,
    PolicyApprovalRequirements,
    PolicyApprovalStatus,
    StoredPolicyApproval,
    approval_fingerprint,
)
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, PolicySourceView
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE, PolicyCatalogService
from aris.modules.nova.policy_history_record_schemas import HistoricalReviewDetailQuery
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService

APPROVE_PERMISSION = "policy.approve"


class PolicyApprovalService:
    def __init__(
        self,
        catalog: PolicyCatalogService,
        history: PolicyHistoricalReviewService,
        requirements: PolicyApprovalRequirements,
        *,
        writing_enabled: bool = False,
    ) -> None:
        if history.catalog is not catalog or requirements.history != history.configuration:
            raise ValueError("approval requires the same catalog and explicit historical methods")
        if type(writing_enabled) is not bool:
            raise ValueError("approval writer must be explicitly enabled")
        self.catalog, self.sessions, self.history = catalog, catalog.sessions, history
        self.requirements = PolicyApprovalRequirements.model_validate(
            requirements.model_dump(mode="json")
        )
        self.writing_enabled = writing_enabled

    def require_writer(self) -> None:
        if not self.writing_enabled:
            raise DomainError(
                "POLICY_APPROVAL_NOT_CONFIGURED", "Source approval is not configured", 503
            )

    def require_source_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        source: PolicySourceReference,
        fingerprint: str,
        *,
        usage_evidence_required: bool = True,
    ) -> PolicySourceView:
        self.catalog.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=actor.actor_id,
                permission=APPROVE_PERMISSION,
                scope=GLOBAL_SCOPE,
            ),
        )
        value = self.catalog.exact_in_transaction(session, actor, source)
        if value.fingerprint != fingerprint:
            raise DomainError("CONFLICT", "Approval source fingerprint differs", 409)
        if usage_evidence_required and value.source.spec.quota.scope == "per_node":
            for node in value.source.spec.quota.node_limits:
                self.catalog.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=actor.actor_id,
                        permission="usage.read_own",
                        scope=AccessScope(type="node", id=node.node_id),
                    ),
                )
        return value

    def _current(self, actor: ActorContext, request: ApprovePolicySource) -> PolicySourceView:
        with self.sessions() as db, db.no_autoflush:
            transaction_budget(db, 100, 1000, 3000)
            return self.require_source_in_transaction(
                db, actor, request.source, request.expected_source_fingerprint
            )

    def preview(self, actor: ActorContext, request: ApprovePolicySource) -> PolicyApprovalPreview:
        self.require_writer()
        request = ApprovePolicySource.model_validate(request.model_dump(mode="json"))
        if (
            request.expected_requirements_fingerprint != self.requirements.fingerprint
            or self.history.configuration != self.requirements.history
        ):
            raise DomainError(
                "CONFLICT", "Approval requirements or historical methods changed", 409
            )
        source = self._current(actor, request)
        spec = source.source.spec
        if (
            spec.quota.scope != "per_node"
            or spec.role_overrides
            or spec.idle_reclaim.mode != "disabled"
        ):
            raise DomainError(
                "POLICY_NOT_SUPPORTED",
                "This approval requires per-node quotas without role overrides or idle reclaim",
                409,
            )
        if (
            not self.requirements.allow_source_author_approval
            and source.created_by == actor.actor_id
        ):
            raise DomainError(
                "PERMISSION_DENIED", "The source author cannot approve their own source", 403
            )
        nodes = {item.node_id for item in spec.quota.node_limits}
        methods = {item.node_id: item for item in self.requirements.history.nodes}
        if {item.node_id for item in request.history} != nodes or not nodes <= methods.keys():
            raise DomainError(
                "VALIDATION_ERROR",
                "Approval needs one configured historical sample for every source node",
                422,
            )
        samples = []
        now = self.catalog._now()
        for ref in request.history:
            stored = self.history.get(
                actor,
                HistoricalReviewDetailQuery(
                    source=request.source,
                    expected_source_fingerprint=request.expected_source_fingerprint,
                    node_id=ref.node_id,
                    id=ref.id,
                    expected_fingerprint=ref.expected_fingerprint,
                ),
            )
            review, query = stored.review, stored.request.query
            evidence, assessment, method = review.evidence, review.assessment, methods[ref.node_id]
            if (
                stored.source_id != source.id
                or stored.request.expected_implementation_ref
                != self.requirements.history.implementation_ref
                or (query.expected_configuration_fingerprint, query.expected_timing_fingerprint)
                != (method.expected_configuration_fingerprint, method.expected_timing_fingerprint)
            ):
                raise DomainError(
                    "CONFLICT",
                    "Approval sample uses a different source or historical implementation",
                    409,
                )
            if (
                query.assumed_roles
                or evidence.coverage != "complete"
                or not evidence.source_prefix_complete
                or evidence.models is None
            ):
                raise DomainError(
                    "POLICY_REVIEW_INCOMPLETE",
                    "Approval sample must have complete coverage and no assumed roles",
                    409,
                )
            if (
                (evidence.window.end_at - evidence.window.start_at)
                < timedelta(seconds=self.requirements.minimum_window_seconds)
                or not timedelta(0)
                <= now - evidence.window.end_at
                <= timedelta(seconds=self.requirements.maximum_history_age_seconds)
                or stored.created_at > now
            ):
                raise DomainError(
                    "POLICY_REVIEW_EXPIRED",
                    "Approval sample is too short, too old or from the future",
                    409,
                )
            if self.requirements.require_evaluated_quota and assessment.state == "not_evaluated":
                raise DomainError(
                    "POLICY_REVIEW_INCOMPLETE",
                    "Approval requires an evaluated historical quota period",
                    409,
                )
            samples.append(
                ApprovedHistoricalSample(
                    reference=ref,
                    context_fingerprint=stored.request.expected_context_fingerprint,
                    snapshot_id=query.usage.snapshot_id,
                    window_start_at=evidence.window.start_at,
                    window_end_at=evidence.window.end_at,
                    saved_at=stored.created_at,
                    quota_state=assessment.state,
                    quota_reason=assessment.reason,
                )
            )
        self._current(actor, request)
        # Recheck the same age boundary after public readers, including time spent waiting.
        completed = self.catalog._now()
        if any(
            not timedelta(0)
            <= completed - item.window_end_at
            <= timedelta(seconds=self.requirements.maximum_history_age_seconds)
            for item in samples
        ):
            raise DomainError(
                "POLICY_REVIEW_EXPIRED", "Historical approval evidence expired during review", 409
            )
        return PolicyApprovalPreview(
            request=request,
            source_id=source.id,
            source_author=source.created_by,
            requirements=self.requirements,
            samples=tuple(samples),
        )

    def _execution_current(
        self, db: Session, execution: CommandExecution, request: ApprovePolicySource
    ) -> None:
        self.require_writer()
        if (
            self.requirements.fingerprint != request.expected_requirements_fingerprint
            or self.history.configuration != self.requirements.history
        ):
            raise DomainError("CONFLICT", "Approval requirements changed", 409)
        if (
            execution.execution_deadline is not None
            and self.catalog._now() >= execution.execution_deadline
        ):
            raise DomainError("EXECUTION_EXPIRED", "Approval execution deadline passed", 409)
        self.require_source_in_transaction(
            db,
            ActorContext(actor_id=execution.actor_id, request_id=execution.request_id),
            request.source,
            request.expected_source_fingerprint,
        )

    def approve_in_transaction(
        self,
        db: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: ApprovePolicySource,
    ) -> PolicyApprovalReceipt:
        if principal.user_id != execution.actor_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError("PERMISSION_DENIED", "Approval actor or scope differs", 403)
        request = ApprovePolicySource.model_validate(request.model_dump(mode="json"))
        transaction_budget(db, 100, 1000, 8000)
        with transaction_savepoint(db):
            self._execution_current(db, execution, request)
            actor = ActorContext(actor_id=execution.actor_id, request_id=execution.request_id)
            preview = self.preview(actor, request)
            id = uuid5(
                NAMESPACE_URL, "aris.nova.policy_source_approval:" + str(execution.command_id)
            )
            repo = PolicyApprovalRepository(db)
            stored, created = repo.get(id, actor.actor_id), False
            if stored is None:
                now = self.catalog._now()
                draft = StoredPolicyApproval.model_construct(
                    id=id,
                    preview=preview,
                    approved_by=actor.actor_id,
                    command_id=execution.command_id,
                    request_id=execution.request_id,
                    approved_at=now,
                    expires_at=now + timedelta(seconds=self.requirements.validity_seconds),
                    fingerprint="0" * 64,
                )
                value = StoredPolicyApproval.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": approval_fingerprint(
                            "record", draft, exclude={"fingerprint"}
                        ),
                    }
                )
                created = repo.insert(value)
                stored = repo.get(id, actor.actor_id)
            self._execution_current(db, execution, request)
            if stored is None or (stored.preview, stored.approved_by, stored.command_id) != (
                preview,
                actor.actor_id,
                execution.command_id,
            ):
                raise DomainError("CONFLICT", "Approval differs from its original command", 409)
            if created:
                self._event(db, execution, stored)
            db.flush()
            self._execution_current(db, execution, request)
            return PolicyApprovalReceipt(
                id=stored.id,
                source=request.source,
                fingerprint=stored.fingerprint,
                expires_at=stored.expires_at,
                created=created,
            )

    def _event(self, db: Session, execution: CommandExecution, value: StoredPolicyApproval) -> None:
        now, event_id, action = self.catalog._now(), str(uuid4()), "policy.source_approved"
        db.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="policy_source_approval",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload={
                    "approval_id": str(value.id),
                    "source_id": str(value.preview.source_id),
                    "fingerprint": value.fingerprint,
                    "publication_authorized": False,
                },
            )
        )
        db.flush()
        db.add(OutboxEntry(event_id=event_id, created_at=now))
        db.add(
            AuditEntry(
                id=str(uuid4()),
                actor=str(execution.actor_id),
                action=action,
                target=str(value.id),
                request_id=execution.request_id,
                recorded_at=now,
                changes={
                    "command_id": str(execution.command_id),
                    "fingerprint": value.fingerprint,
                    "reason": value.preview.request.reason,
                    "requirements_fingerprint": value.preview.requirements.fingerprint,
                },
            )
        )

    def _read(
        self,
        actor: ActorContext,
        query: PolicyApprovalListQuery | PolicyApprovalDetailQuery,
        *,
        include_status: bool = False,
    ) -> PolicyApprovalPage | StoredPolicyApproval | PolicyApprovalStatus:
        started = monotonic()
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                source = self.require_source_in_transaction(
                    db, actor, query.source, query.expected_source_fingerprint
                )
                repo = PolicyApprovalRepository(db)
                if isinstance(query, PolicyApprovalListQuery):
                    values = repo.page(query, source.id, actor.actor_id)
                    if any(
                        (item.source, item.source_fingerprint) != (query.source, source.fingerprint)
                        for item in values
                    ):
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE", "Approval directory differs", 503
                        )
                    result: PolicyApprovalPage | StoredPolicyApproval | PolicyApprovalStatus = (
                        PolicyApprovalPage(
                            query=query,
                            as_of=self.catalog._now(),
                            items=values[: query.limit],
                            next_cursor=PolicyApprovalCursor(
                                approved_at=values[query.limit - 1].approved_at,
                                id=values[query.limit - 1].id,
                            )
                            if len(values) > query.limit
                            else None,
                        )
                    )
                else:
                    value = repo.get(query.id, actor.actor_id)
                    if value is None:
                        raise DomainError("NOT_FOUND", "Source approval was not found", 404)
                    if (
                        value.preview.source_id,
                        value.preview.request.source,
                        value.preview.request.expected_source_fingerprint,
                        value.fingerprint,
                    ) != (source.id, query.source, source.fingerprint, query.expected_fingerprint):
                        raise DomainError("CONFLICT", "Source approval reference differs", 409)
                    result = value
                    if include_status:
                        revocation = PolicyApprovalRevocationRepository(db).get(
                            value.id, actor.actor_id
                        )
                        now = self.catalog._now()
                        result = PolicyApprovalStatus(
                            as_of=now,
                            record=value,
                            revocation=revocation,
                            state="revoked"
                            if revocation is not None
                            else "expired"
                            if now >= value.expires_at
                            else "recorded",
                            current_requirements_match=value.preview.requirements
                            == self.requirements,
                        )
            with self.sessions() as db, db.no_autoflush:
                transaction_budget(db, 100, 1000, 3000)
                self.require_source_in_transaction(
                    db, actor, query.source, query.expected_source_fingerprint
                )
            if monotonic() - started >= 3:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Approval read budget exceeded", 503)
            return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Approval database is unavailable",
                503,
            ) from None

    def list(self, actor: ActorContext, query: PolicyApprovalListQuery) -> PolicyApprovalPage:
        result = self._read(
            actor, PolicyApprovalListQuery.model_validate(query.model_dump(mode="json"))
        )
        assert isinstance(result, PolicyApprovalPage)
        return result

    def get(self, actor: ActorContext, query: PolicyApprovalDetailQuery) -> StoredPolicyApproval:
        result = self._read(
            actor, PolicyApprovalDetailQuery.model_validate(query.model_dump(mode="json"))
        )
        assert isinstance(result, StoredPolicyApproval)
        return result

    def status(self, actor: ActorContext, query: PolicyApprovalDetailQuery) -> PolicyApprovalStatus:
        result = self._read(
            actor,
            PolicyApprovalDetailQuery.model_validate(query.model_dump(mode="json")),
            include_status=True,
        )
        assert isinstance(result, PolicyApprovalStatus)
        return result
