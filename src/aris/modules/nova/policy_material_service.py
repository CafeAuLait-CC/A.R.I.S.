"""Record supplied-case material through Core; approval and publication remain separate."""

from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessRequest, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, PolicySourceView
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE, PolicyCatalogService
from aris.modules.nova.policy_material_repo import PolicyMaterialRepository
from aris.modules.nova.policy_material_schemas import (
    ImplementationReference,
    PolicyMaterialCursor,
    PolicyMaterialListQuery,
    PolicyMaterialPage,
    PolicyReviewMaterialReceipt,
    RecordPolicyReviewMaterial,
    StoredPolicyReviewMaterial,
    policy_material_fingerprint,
)
from aris.modules.nova.policy_simulation import simulate_policy_constraints
from aris.modules.nova.policy_source import compile_policy

_implementation = TypeAdapter(ImplementationReference)


class PolicyReviewMaterialService:
    def __init__(
        self, catalog: PolicyCatalogService, *, implementation_ref: str | None = None
    ) -> None:
        self.catalog, self.sessions = catalog, catalog.sessions
        self.implementation_ref = (
            _implementation.validate_python(implementation_ref)
            if implementation_ref is not None
            else None
        )

    def require_writer(self, expected: str | None = None) -> str:
        if self.implementation_ref is None:
            raise DomainError(
                "POLICY_REVIEW_NOT_CONFIGURED", "Policy material writing is not configured", 503
            )
        if expected is not None and expected != self.implementation_ref:
            raise DomainError("CONFLICT", "Policy review implementation reference changed", 409)
        return self.implementation_ref

    def _source(
        self,
        session: Session,
        actor: ActorContext,
        reference: PolicySourceReference,
        fingerprint: str,
    ) -> PolicySourceView:
        value = self.catalog.exact_in_transaction(session, actor, reference)
        if value.fingerprint != fingerprint:
            raise DomainError("CONFLICT", "Policy review source fingerprint differs", 409)
        return value

    @staticmethod
    def _receipt(value: StoredPolicyReviewMaterial, created: bool) -> PolicyReviewMaterialReceipt:
        return PolicyReviewMaterialReceipt(
            id=value.id, source=value.request.source, fingerprint=value.fingerprint, created=created
        )

    def _execution_current(
        self, session: Session, execution: CommandExecution, request: RecordPolicyReviewMaterial
    ) -> None:
        self._source(session, execution, request.source, request.expected_source_fingerprint)
        self.require_writer(request.expected_implementation_ref)
        if (
            execution.execution_deadline is not None
            and self.catalog._now() >= execution.execution_deadline
        ):
            raise DomainError("EXECUTION_EXPIRED", "Policy material execution deadline passed", 409)

    def record_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: RecordPolicyReviewMaterial,
    ) -> PolicyReviewMaterialReceipt:
        request = RecordPolicyReviewMaterial.model_validate(request.model_dump(mode="json"))
        implementation = self.require_writer(request.expected_implementation_ref)
        if principal.user_id != execution.actor_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError("PERMISSION_DENIED", "Policy material actor or scope differs", 403)
        transaction_budget(session, 100, 1000, 3000)
        with transaction_savepoint(session):
            source = self._source(
                session, execution, request.source, request.expected_source_fingerprint
            )
            repo = PolicyMaterialRepository(session)
            id = uuid5(
                NAMESPACE_URL, "aris.nova.policy_review_material:" + str(execution.command_id)
            )
            prior = repo.get(id)
            if prior is not None:
                if (
                    prior.request,
                    prior.created_by,
                    prior.command_id,
                    prior.source_id,
                    prior.implementation_ref,
                ) != (request, execution.actor_id, execution.command_id, source.id, implementation):
                    raise DomainError(
                        "CONFLICT", "Policy material command has different input", 409
                    )
                self._execution_current(session, execution, request)
                return self._receipt(prior, False)
            compiled = compile_policy(source.source)
            results = tuple(
                simulate_policy_constraints(compiled, case) for case in request.scenarios
            )
            now = self.catalog._now()
            if now < source.created_at:
                raise DomainError("CONFLICT", "Policy material clock precedes its source", 409)
            draft = StoredPolicyReviewMaterial.model_construct(
                id=id,
                source_id=source.id,
                request=request,
                implementation_ref=implementation,
                results=results,
                created_by=execution.actor_id,
                command_id=execution.command_id,
                request_id=execution.request_id,
                created_at=now,
                fingerprint="0" * 64,
            )
            value = StoredPolicyReviewMaterial.model_validate(
                {**draft.model_dump(mode="json"), "fingerprint": policy_material_fingerprint(draft)}
            )
            created = repo.insert_if_absent(value)
            self._execution_current(session, execution, request)
            stored = repo.get(id)
            if stored is None or (
                stored.request,
                stored.source_id,
                stored.created_by,
                stored.command_id,
                stored.implementation_ref,
            ) != (request, source.id, execution.actor_id, execution.command_id, implementation):
                raise DomainError(
                    "CONFLICT", "Stored policy material differs from the original command", 409
                )
            if created:
                self._append_event(session, execution, stored)
            session.flush()
            return self._receipt(stored, created)

    def _append_event(
        self, session: Session, execution: CommandExecution, value: StoredPolicyReviewMaterial
    ) -> None:
        now = self.catalog._now()
        action = "policy.review_material_recorded"
        event_id = str(uuid4())
        payload = {
            "material_id": str(value.id),
            "source_id": str(value.source_id),
            "name": value.request.source.name,
            "source_version": value.request.source.version,
            "fingerprint": value.fingerprint,
            "examples": len(value.results),
            "review_material_only": True,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="policy_review_material",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload=payload,
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
                    "implementation_ref": value.implementation_ref,
                },
            )
        )

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Policy review material database is unavailable",
            503,
        )

    def read(
        self,
        actor: ActorContext,
        id: UUID,
        *,
        reverify: bool = False,
        expected_source: PolicySourceReference | None = None,
    ) -> StoredPolicyReviewMaterial:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        if reverify:
            self.require_writer()
        deadline = monotonic() + 3
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(session, 100, 1000, 3000)
                # Unknown IDs never expose a body before current global draft access.
                self.catalog.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=actor.actor_id,
                        permission="policy.manage_drafts",
                        scope=GLOBAL_SCOPE,
                    ),
                )
                repo = PolicyMaterialRepository(session)
                if expected_source is not None and repo.scope(id) != expected_source:
                    raise DomainError(
                        "NOT_FOUND", "Material does not belong to the selected source", 404
                    )
                value = repo.get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Policy review material is unknown", 404)
                source = self._source(
                    session, actor, value.request.source, value.request.expected_source_fingerprint
                )
                if source.id != value.source_id:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Policy material source identity differs", 503
                    )
                if reverify:
                    self.require_writer(value.implementation_ref)
                    compiled = compile_policy(source.source)
                    results = tuple(
                        simulate_policy_constraints(compiled, case)
                        for case in value.request.scenarios
                    )
                    if results != value.results:
                        raise DomainError(
                            "CONFLICT",
                            "Review examples differ under the configured implementation",
                            409,
                        )
                self._source(
                    session, actor, value.request.source, value.request.expected_source_fingerprint
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Policy material read budget exceeded", 503
                    )
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def list(self, actor: ActorContext, query: PolicyMaterialListQuery) -> PolicyMaterialPage:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = PolicyMaterialListQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 3
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(session, 100, 1000, 3000)
                source = self._source(
                    session, actor, query.source, query.expected_source_fingerprint
                )
                values = PolicyMaterialRepository(session).page(query, source.id)
                if any(
                    (value.source, value.source_id, value.source_fingerprint)
                    != (query.source, source.id, source.fingerprint)
                    for value in values
                ):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Policy material directory source differs", 503
                    )
                result = PolicyMaterialPage(
                    query=query,
                    items=values[: query.limit],
                    next_cursor=PolicyMaterialCursor(
                        created_at=values[query.limit - 1].created_at, id=values[query.limit - 1].id
                    )
                    if len(values) > query.limit
                    else None,
                )
                self._source(session, actor, query.source, query.expected_source_fingerprint)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Policy material directory budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            raise self._failure(error) from None
