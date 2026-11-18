"""Scoped read-only ledger reports. Source completeness remains unestablished."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.recorded_usage import (
    RecordedUsageMetrics,
    RecordedUsageQuery,
    recorded_usage_metrics,
)

ReadPermission = Literal["usage.read_own", "usage.read_all"]


class RecordedUsageService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 3000,
    ) -> None:
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000:
            raise ValueError("recorded usage requires a bounded read timeout")
        self.sessions = sessions
        self.access = AccessService(sessions)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("recorded usage clock requires a timezone")
        return now.astimezone(UTC)

    def mine(self, actor: ActorContext, query: RecordedUsageQuery) -> RecordedUsageMetrics:
        with self.sessions() as session:
            return self.mine_in_transaction(session, actor, query)

    def mine_in_transaction(
        self, session: Session, actor: ActorContext, query: RecordedUsageQuery
    ) -> RecordedUsageMetrics:
        return self._read(session, actor, query, actor.actor_id, "usage.read_own")

    def for_user(
        self, actor: ActorContext, owner_id: UUID, query: RecordedUsageQuery
    ) -> RecordedUsageMetrics:
        with self.sessions() as session:
            return self._read(session, actor, query, owner_id, "usage.read_all")

    def _read(
        self,
        session: Session,
        actor: ActorContext,
        query: RecordedUsageQuery,
        owner_id: UUID,
        permission: ReadPermission,
    ) -> RecordedUsageMetrics:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = RecordedUsageQuery.model_validate(query.model_dump(mode="json"))
        owner_id = UUID(str(owner_id))
        access = AccessRequest(
            user_id=actor.actor_id,
            permission=permission,
            scope=AccessScope(type="node", id=query.node_id),
        )
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                transaction_budget(
                    session, min(100, self.timeout_ms), min(1000, self.timeout_ms), self.timeout_ms
                )
                self.access.require_in_transaction(session, access)
                if query.end_at > self._now():
                    raise DomainError("VALIDATION_ERROR", "Usage window has not ended", 422)
                repo = UsageLedgerRepository(session)
                current = repo.head(query.node_id)
                head = query.snapshot or current
                if head.revision > current.revision:
                    raise DomainError("CONFLICT", "Usage snapshot has not been committed", 409)
                entries = repo.prefix(head)
                result = recorded_usage_metrics(
                    query, owner_id=owner_id, head=head, entries=entries, read_at=self._now()
                )
                self.access.require_in_transaction(session, access)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Usage report read budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Usage report database is unavailable",
                503,
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None
