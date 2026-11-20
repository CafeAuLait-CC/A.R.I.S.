"""Save server-derived historical evidence without giving it approval authority."""

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
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, PolicySourceView
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE, PolicyCatalogService
from aris.modules.nova.policy_history_record_repo import PolicyHistoricalReviewRepository
from aris.modules.nova.policy_history_record_schemas import (
    HistoricalReviewConfiguration,
    HistoricalReviewCursor,
    HistoricalReviewDetailQuery,
    HistoricalReviewListQuery,
    HistoricalReviewPage,
    HistoricalReviewReceipt,
    RecordHistoricalPolicyReview,
    StoredHistoricalPolicyReview,
    historical_context_fingerprint,
    history_record_fingerprint,
)
from aris.modules.nova.quota_history_schemas import HistoricalQuotaReview
from aris.modules.nova.quota_history_service import HistoricalQuotaAssessmentService


class PolicyHistoricalReviewService:
    def __init__(
        self,
        catalog: PolicyCatalogService,
        assessments: HistoricalQuotaAssessmentService,
        configuration: HistoricalReviewConfiguration,
        *,
        writing_enabled: bool = False,
    ) -> None:
        if assessments.catalog is not catalog or type(writing_enabled) is not bool:
            raise ValueError("historical records require the same catalog and explicit writer")
        self.catalog, self.sessions, self.assessments = catalog, catalog.sessions, assessments
        self.configuration = HistoricalReviewConfiguration.model_validate(
            configuration.model_dump(mode="json")
        )
        self.writing_enabled = writing_enabled

    def require_writer(self) -> None:
        if not self.writing_enabled:
            raise DomainError(
                "POLICY_HISTORY_NOT_CONFIGURED", "Historical review storage is not configured", 503
            )

    def _scope(
        self,
        session: Session,
        actor: ActorContext,
        source: PolicySourceReference,
        fingerprint: str,
        node_id: str,
    ) -> PolicySourceView:
        if node_id not in {node.node_id for node in self.configuration.nodes}:
            raise DomainError("PERMISSION_DENIED", "Historical node is outside this interface", 403)
        value = self.catalog.exact_in_transaction(session, actor, source)
        if value.fingerprint != fingerprint:
            raise DomainError("CONFLICT", "Historical review source fingerprint differs", 409)
        self.catalog.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=actor.actor_id,
                permission="usage.read_own",
                scope=AccessScope(type="node", id=node_id),
            ),
        )
        return value

    def _configured(self, actor: ActorContext, request: RecordHistoricalPolicyReview) -> None:
        self.require_writer()
        query = request.query
        if query.usage.owner_id != actor.actor_id:
            raise DomainError(
                "PERMISSION_DENIED", "Historical review must belong to its actor", 403
            )
        node = next(
            (node for node in self.configuration.nodes if node.node_id == query.usage.node_id), None
        )
        if node is None:
            raise DomainError("PERMISSION_DENIED", "Historical node is outside this interface", 403)
        if request.expected_implementation_ref != self.configuration.implementation_ref or (
            query.expected_configuration_fingerprint,
            query.expected_timing_fingerprint,
        ) != (node.expected_configuration_fingerprint, node.expected_timing_fingerprint):
            raise DomainError("CONFLICT", "Historical review method or implementation changed", 409)

    def _current_scope(self, actor: ActorContext, request: RecordHistoricalPolicyReview) -> None:
        self._configured(actor, request)
        with self.sessions() as session, session.no_autoflush:
            transaction_budget(session, 100, 1000, 3000)
            self._scope(
                session,
                actor,
                request.query.policy,
                request.query.expected_policy_fingerprint,
                request.query.usage.node_id,
            )

    def preview(
        self, actor: ActorContext, request: RecordHistoricalPolicyReview
    ) -> HistoricalQuotaReview:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        request = RecordHistoricalPolicyReview.model_validate(request.model_dump(mode="json"))
        self._current_scope(actor, request)
        result = self.assessments.review(actor, request.query)
        if (
            historical_context_fingerprint(result, self.configuration.implementation_ref)
            != request.expected_context_fingerprint
        ):
            raise DomainError(
                "CONFLICT", "Historical context changed; read and review it again", 409
            )
        self._current_scope(actor, request)
        return result

    def _execution_current(
        self, session: Session, execution: CommandExecution, request: RecordHistoricalPolicyReview
    ) -> PolicySourceView:
        actor = ActorContext(actor_id=execution.actor_id, request_id=execution.request_id)
        self._configured(actor, request)
        if (
            execution.execution_deadline is not None
            and self.catalog._now() >= execution.execution_deadline
        ):
            raise DomainError(
                "EXECUTION_EXPIRED", "Historical review execution deadline passed", 409
            )
        return self._scope(
            session,
            actor,
            request.query.policy,
            request.query.expected_policy_fingerprint,
            request.query.usage.node_id,
        )

    def record_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: RecordHistoricalPolicyReview,
    ) -> HistoricalReviewReceipt:
        request = RecordHistoricalPolicyReview.model_validate(request.model_dump(mode="json"))
        if principal.user_id != execution.actor_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError("PERMISSION_DENIED", "Historical review actor or scope differs", 403)
        transaction_budget(session, 100, 1000, 5000)
        with transaction_savepoint(session):
            source = self._execution_current(session, execution, request)
            actor = ActorContext(actor_id=execution.actor_id, request_id=execution.request_id)
            review = self.preview(actor, request)
            id = uuid5(
                NAMESPACE_URL, "aris.nova.historical_policy_review:" + str(execution.command_id)
            )
            repo = PolicyHistoricalReviewRepository(session)
            stored = repo.get(id, actor.actor_id)
            created = False
            if stored is None:
                draft = StoredHistoricalPolicyReview.model_construct(
                    id=id,
                    source_id=source.id,
                    request=request,
                    review=review,
                    created_by=actor.actor_id,
                    command_id=execution.command_id,
                    request_id=execution.request_id,
                    created_at=self.catalog._now(),
                    fingerprint="0" * 64,
                )
                value = StoredHistoricalPolicyReview.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": history_record_fingerprint(draft),
                    }
                )
                created = repo.insert(value)
                stored = repo.get(id, actor.actor_id)
            self._execution_current(session, execution, request)
            if stored is None or (
                stored.request,
                stored.source_id,
                stored.created_by,
                stored.command_id,
            ) != (request, source.id, actor.actor_id, execution.command_id):
                raise DomainError(
                    "CONFLICT", "Historical review differs from the original command", 409
                )
            if created:
                self._event(session, execution, stored)
            session.flush()
            self._execution_current(session, execution, request)
            return HistoricalReviewReceipt(
                id=stored.id,
                source=request.query.policy,
                context_fingerprint=request.expected_context_fingerprint,
                fingerprint=stored.fingerprint,
                created=created,
            )

    def _event(
        self, session: Session, execution: CommandExecution, value: StoredHistoricalPolicyReview
    ) -> None:
        now, event_id = self.catalog._now(), str(uuid4())
        action = "policy.historical_review_recorded"
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="policy_historical_review",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload={
                    "review_id": str(value.id),
                    "source_id": str(value.source_id),
                    "fingerprint": value.fingerprint,
                    "historical_only": True,
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
                    "fingerprint": value.fingerprint,
                    "context_fingerprint": value.request.expected_context_fingerprint,
                    "reason": value.request.reason,
                },
            )
        )

    def _read(
        self, actor: ActorContext, query: HistoricalReviewListQuery | HistoricalReviewDetailQuery
    ) -> HistoricalReviewPage | StoredHistoricalPolicyReview:
        started = monotonic()
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(session, 100, 1000, 3000)
                source = self._scope(
                    session, actor, query.source, query.expected_source_fingerprint, query.node_id
                )
                repo = PolicyHistoricalReviewRepository(session)
                if isinstance(query, HistoricalReviewListQuery):
                    values = repo.page(query, source.id, actor.actor_id)
                    if any(
                        (item.source, item.source_fingerprint, item.node_id)
                        != (query.source, source.fingerprint, query.node_id)
                        for item in values
                    ):
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE", "Historical review directory differs", 503
                        )
                    result: HistoricalReviewPage | StoredHistoricalPolicyReview = (
                        HistoricalReviewPage(
                            query=query,
                            items=values[: query.limit],
                            next_cursor=HistoricalReviewCursor(
                                created_at=values[query.limit - 1].created_at,
                                id=values[query.limit - 1].id,
                            )
                            if len(values) > query.limit
                            else None,
                        )
                    )
                else:
                    stored = repo.get(query.id, actor.actor_id)
                    if stored is None:
                        raise DomainError("NOT_FOUND", "Historical review was not found", 404)
                    if (
                        stored.source_id,
                        stored.request.query.policy,
                        stored.request.query.expected_policy_fingerprint,
                        stored.request.query.usage.node_id,
                        stored.fingerprint,
                    ) != (
                        source.id,
                        query.source,
                        source.fingerprint,
                        query.node_id,
                        query.expected_fingerprint,
                    ):
                        raise DomainError(
                            "CONFLICT", "Historical review source or fingerprint differs", 409
                        )
                    result = stored
            with self.sessions() as current, current.no_autoflush:
                transaction_budget(current, 100, 1000, 3000)
                self._scope(
                    current, actor, query.source, query.expected_source_fingerprint, query.node_id
                )
            if monotonic() - started >= 3:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Historical review read budget exceeded", 503
                )
            return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Historical review database is unavailable",
                503,
            ) from None

    def list(self, actor: ActorContext, query: HistoricalReviewListQuery) -> HistoricalReviewPage:
        query = HistoricalReviewListQuery.model_validate(query.model_dump(mode="json"))
        result = self._read(actor, query)
        assert isinstance(result, HistoricalReviewPage)
        return result

    def get(
        self, actor: ActorContext, query: HistoricalReviewDetailQuery
    ) -> StoredHistoricalPolicyReview:
        query = HistoricalReviewDetailQuery.model_validate(query.model_dump(mode="json"))
        result = self._read(actor, query)
        assert isinstance(result, StoredHistoricalPolicyReview)
        return result
