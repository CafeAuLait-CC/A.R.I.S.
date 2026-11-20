"""A person can retract their own approval without rewriting review history."""

from time import monotonic
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.policy_approval_repo import PolicyApprovalRepository
from aris.modules.nova.policy_approval_revocation_repo import PolicyApprovalRevocationRepository
from aris.modules.nova.policy_approval_schemas import (
    PolicyApprovalRevocationPreview,
    PolicyApprovalRevocationReceipt,
    RevokePolicySourceApproval,
    StoredPolicyApproval,
    StoredPolicyApprovalRevocation,
    approval_fingerprint,
)
from aris.modules.nova.policy_approval_service import PolicyApprovalService
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE


class PolicyApprovalRevocationService:
    def __init__(self, approvals: PolicyApprovalService) -> None:
        self.approvals, self.sessions, self.catalog = (
            approvals,
            approvals.sessions,
            approvals.catalog,
        )

    def _authorize(
        self, db: Session, actor: ActorContext, request: RevokePolicySourceApproval
    ) -> None:
        self.approvals.require_writer()
        # Retraction does not renew or reinterpret evidence. Lost usage access,
        # changed review methods and expired samples cannot force an endorsement to stay.
        self.approvals.require_source_in_transaction(
            db,
            actor,
            request.source,
            request.expected_source_fingerprint,
            usage_evidence_required=False,
        )

    def _original(
        self, db: Session, actor: ActorContext, request: RevokePolicySourceApproval
    ) -> StoredPolicyApproval:
        self._authorize(db, actor, request)
        approval = PolicyApprovalRepository(db).get(request.approval_id, actor.actor_id)
        if approval is None:
            raise DomainError("NOT_FOUND", "Own source approval was not found", 404)
        if (
            approval.preview.request.source,
            approval.preview.request.expected_source_fingerprint,
            approval.fingerprint,
        ) != (
            request.source,
            request.expected_source_fingerprint,
            request.expected_approval_fingerprint,
        ) or approval.approved_at > self.catalog._now():
            raise DomainError("CONFLICT", "Approval revocation reference differs", 409)
        return approval

    def preview(
        self, actor: ActorContext, request: RevokePolicySourceApproval
    ) -> PolicyApprovalRevocationPreview:
        request = RevokePolicySourceApproval.model_validate(request.model_dump(mode="json"))
        started = monotonic()
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                approval = self._original(db, actor, request)
                old = PolicyApprovalRevocationRepository(db).get(approval.id, actor.actor_id)
                result = PolicyApprovalRevocationPreview(
                    request=request,
                    approved_at=approval.approved_at,
                    expires_at=approval.expires_at,
                    existing_revocation_id=old.id if old is not None else None,
                )
            with self.sessions() as db, db.no_autoflush:
                transaction_budget(db, 100, 1000, 3000)
                self._authorize(db, actor, request)
            if monotonic() - started >= 3:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Approval revocation read budget exceeded", 503
                )
            return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Approval revocation database is unavailable",
                503,
            ) from None

    def _current(
        self, db: Session, execution: CommandExecution, request: RevokePolicySourceApproval
    ) -> None:
        self._authorize(
            db, ActorContext(actor_id=execution.actor_id, request_id=execution.request_id), request
        )
        if (
            execution.execution_deadline is not None
            and self.catalog._now() >= execution.execution_deadline
        ):
            raise DomainError(
                "EXECUTION_EXPIRED", "Approval revocation execution deadline passed", 409
            )

    def revoke_in_transaction(
        self,
        db: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: RevokePolicySourceApproval,
    ) -> PolicyApprovalRevocationReceipt:
        if execution.actor_id != principal.user_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError(
                "PERMISSION_DENIED", "Approval revocation actor or scope differs", 403
            )
        request = RevokePolicySourceApproval.model_validate(request.model_dump(mode="json"))
        transaction_budget(db, 100, 1000, 5000)
        with transaction_savepoint(db):
            self._current(db, execution, request)
            actor = ActorContext(actor_id=execution.actor_id, request_id=execution.request_id)
            approval = self._original(db, actor, request)
            repo = PolicyApprovalRevocationRepository(db)
            stored, created = repo.get(approval.id, actor.actor_id), False
            if stored is None:
                draft = StoredPolicyApprovalRevocation.model_construct(
                    id=uuid5(
                        NAMESPACE_URL,
                        "aris.nova.policy_approval_revocation:" + str(execution.command_id),
                    ),
                    request=request,
                    revoked_by=actor.actor_id,
                    command_id=execution.command_id,
                    request_id=execution.request_id,
                    revoked_at=self.catalog._now(),
                    fingerprint="0" * 64,
                )
                value = StoredPolicyApprovalRevocation.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": approval_fingerprint(
                            "revocation", draft, exclude={"fingerprint"}
                        ),
                    }
                )
                created = repo.insert(value)
                stored = repo.get(approval.id, actor.actor_id)
            self._current(db, execution, request)
            if stored is None or (
                stored.request.source,
                stored.request.expected_source_fingerprint,
                stored.request.approval_id,
                stored.request.expected_approval_fingerprint,
                stored.revoked_by,
            ) != (
                request.source,
                request.expected_source_fingerprint,
                request.approval_id,
                request.expected_approval_fingerprint,
                actor.actor_id,
            ):
                raise DomainError("CONFLICT", "Original approval revocation differs", 409)
            # Two independently confirmed retractions keep the first original reason.
            if created:
                self._event(db, execution, stored)
            db.flush()
            self._current(db, execution, request)
            return PolicyApprovalRevocationReceipt(
                id=stored.id,
                source=request.source,
                approval_id=approval.id,
                fingerprint=stored.fingerprint,
                created=created,
            )

    def _event(
        self, db: Session, execution: CommandExecution, value: StoredPolicyApprovalRevocation
    ) -> None:
        now, event_id, action = self.catalog._now(), str(uuid4()), "policy.source_approval_revoked"
        db.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="policy_source_approval_revocation",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload={
                    "revocation_id": str(value.id),
                    "approval_id": str(value.request.approval_id),
                    "fingerprint": value.fingerprint,
                    "publication_performed": False,
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
                target=str(value.request.approval_id),
                request_id=execution.request_id,
                recorded_at=now,
                changes={
                    "command_id": str(execution.command_id),
                    "revocation_id": str(value.id),
                    "fingerprint": value.fingerprint,
                    "reason": value.request.reason,
                },
            )
        )
