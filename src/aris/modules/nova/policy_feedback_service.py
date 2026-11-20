"""Record and read human feedback against the original immutable source."""

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
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, PolicySourceView
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE, PolicyCatalogService
from aris.modules.nova.policy_feedback_repo import PolicyFeedbackRepository
from aris.modules.nova.policy_feedback_schemas import (
    PolicyFeedbackCursor,
    PolicyFeedbackListQuery,
    PolicyFeedbackPage,
    PolicyFeedbackReceipt,
    RecordPolicyFeedback,
    StoredPolicyFeedback,
    policy_feedback_fingerprint,
)
from aris.modules.nova.policy_material_repo import PolicyMaterialRepository


class PolicyFeedbackService:
    def __init__(self, catalog: PolicyCatalogService, *, writing_enabled: bool = False) -> None:
        self.catalog, self.sessions = catalog, catalog.sessions
        self.writing_enabled = writing_enabled

    def require_writer(self) -> None:
        if not self.writing_enabled:
            raise DomainError(
                "POLICY_FEEDBACK_NOT_CONFIGURED", "Human feedback writing is not configured", 503
            )

    def _source(
        self, session: Session, actor: ActorContext, source: PolicySourceReference, fingerprint: str
    ) -> PolicySourceView:
        value = self.catalog.exact_in_transaction(session, actor, source)
        if value.fingerprint != fingerprint:
            raise DomainError("CONFLICT", "Feedback source fingerprint differs", 409)
        return value

    def validate_in_transaction(
        self, session: Session, actor: ActorContext, request: RecordPolicyFeedback
    ) -> PolicySourceView:
        source = self._source(session, actor, request.source, request.expected_source_fingerprint)
        now = self.catalog._now()
        if now < source.created_at:
            raise DomainError("CONFLICT", "Feedback clock precedes its source", 409)
        if request.material is not None:
            material = PolicyMaterialRepository(session).get(request.material.id)
            if material is None or (
                material.source_id,
                material.request.source,
                material.request.expected_source_fingerprint,
                material.fingerprint,
            ) != (source.id, request.source, source.fingerprint, request.material.fingerprint):
                raise DomainError("CONFLICT", "Feedback material does not match its source", 409)
            if now < material.created_at:
                raise DomainError("CONFLICT", "Feedback clock precedes its material", 409)
        return source

    def _current(
        self, session: Session, execution: CommandExecution, request: RecordPolicyFeedback
    ) -> PolicySourceView:
        self.require_writer()
        source = self.validate_in_transaction(session, execution, request)
        if (
            execution.execution_deadline is not None
            and self.catalog._now() >= execution.execution_deadline
        ):
            raise DomainError("EXECUTION_EXPIRED", "Feedback execution deadline passed", 409)
        return source

    def record_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: RecordPolicyFeedback,
    ) -> PolicyFeedbackReceipt:
        request = RecordPolicyFeedback.model_validate(request.model_dump(mode="json"))
        if principal.user_id != execution.actor_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError("PERMISSION_DENIED", "Feedback actor or scope differs", 403)
        transaction_budget(session, 100, 1000, 3000)
        with transaction_savepoint(session):
            source = self._current(session, execution, request)
            id = uuid5(NAMESPACE_URL, "aris.nova.policy_feedback:" + str(execution.command_id))
            repo = PolicyFeedbackRepository(session)
            stored = repo.get(id)
            created = False
            if stored is None:
                draft = StoredPolicyFeedback.model_construct(
                    id=id,
                    source_id=source.id,
                    request=request,
                    created_by=execution.actor_id,
                    command_id=execution.command_id,
                    request_id=execution.request_id,
                    created_at=self.catalog._now(),
                    fingerprint="0" * 64,
                )
                value = StoredPolicyFeedback.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": policy_feedback_fingerprint(draft),
                    }
                )
                created = repo.insert(value)
                stored = repo.get(id)
            self._current(session, execution, request)
            if stored is None or (
                stored.request,
                stored.source_id,
                stored.created_by,
                stored.command_id,
            ) != (request, source.id, execution.actor_id, execution.command_id):
                raise DomainError("CONFLICT", "Feedback differs from the original command", 409)
            if created:
                self._append_event(session, execution, stored)
            session.flush()
            self._current(session, execution, request)
            return PolicyFeedbackReceipt(
                id=stored.id,
                source=request.source,
                fingerprint=stored.fingerprint,
                created=created,
            )

    def _append_event(
        self, session: Session, execution: CommandExecution, value: StoredPolicyFeedback
    ) -> None:
        now, event_id = self.catalog._now(), str(uuid4())
        action = "policy.feedback_recorded"
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="policy_feedback",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload={
                    "feedback_id": str(value.id),
                    "source_id": str(value.source_id),
                    "name": value.request.source.name,
                    "source_version": value.request.source.version,
                    "kind": value.request.kind,
                    "fingerprint": value.fingerprint,
                    "feedback_only": True,
                },
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=now))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=str(execution.actor_id),
                action=action,
                target=str(value.id),
                request_id=execution.request_id,
                recorded_at=now,
                changes={
                    "command_id": str(execution.command_id),
                    "source_fingerprint": value.request.expected_source_fingerprint,
                    "fingerprint": value.fingerprint,
                    "reason": value.request.reason,
                },
            )
        )

    def list_in_transaction(
        self, session: Session, actor: ActorContext, query: PolicyFeedbackListQuery
    ) -> PolicyFeedbackPage:
        query = PolicyFeedbackListQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 3
        with session.no_autoflush:
            transaction_budget(session, 100, 1000, 3000)
            source = self._source(session, actor, query.source, query.expected_source_fingerprint)
            values = PolicyFeedbackRepository(session).page(query, source.id)
            if any(
                (value.source_id, value.request.source, value.request.expected_source_fingerprint)
                != (source.id, query.source, source.fingerprint)
                for value in values
            ):
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Feedback directory differs", 503)
            self._source(session, actor, query.source, query.expected_source_fingerprint)
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Feedback read budget exceeded", 503)
            return PolicyFeedbackPage(
                query=query,
                items=values[: query.limit],
                next_cursor=PolicyFeedbackCursor(
                    created_at=values[query.limit - 1].created_at, id=values[query.limit - 1].id
                )
                if len(values) > query.limit
                else None,
            )

    def list(self, actor: ActorContext, query: PolicyFeedbackListQuery) -> PolicyFeedbackPage:
        try:
            with self.sessions() as session:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                return self.list_in_transaction(session, actor, query)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Policy feedback database is unavailable",
                503,
            ) from None
