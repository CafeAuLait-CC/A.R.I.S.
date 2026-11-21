"""Currently-authorized statistics using bounded window indexes, with no index writes."""

from collections.abc import Callable
from datetime import datetime
from time import monotonic
from typing import Literal, TypeVar
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.continuous_usage import (
    ContinuousUsageHistory,
    UsageRowContext,
    continuous_usage_history,
)
from aris.modules.lyra.indexed_usage_history import (
    IndexedUsageSummary,
    indexed_usage_history,
    indexed_usage_summary,
)
from aris.modules.lyra.ledger_repo import inconsistent
from aris.modules.lyra.ledger_segment_schemas import (
    LedgerSegmentConfiguration,
    ledger_read_configuration,
)
from aris.modules.lyra.ledger_window_read import LedgerWindowQuery, LedgerWindowRead
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryQuery
from aris.modules.lyra.usage_history_service import UsageHistoryService

_Result = TypeVar("_Result")


class IndexedUsageHistoryService(UsageHistoryService):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 3000,
        index_configuration: Callable[
            [str], LedgerSegmentConfiguration
        ] = ledger_read_configuration,
    ) -> None:
        super().__init__(sessions, clock=clock, timeout_ms=timeout_ms)
        self.index_configuration = index_configuration

    def _window_in_transaction(
        self,
        session: Session,
        query: UsageHistoryQuery,
        *,
        owner_id: UUID | None,
        deadline: float,
    ) -> LedgerWindowRead:
        reader = LedgerWindowReader(
            self.sessions,
            self.index_configuration(query.node_id),
            clock=self.clock,
            timeout_ms=self.timeout_ms,
        )
        return reader.read_in_transaction(
            session,
            LedgerWindowQuery(**query.model_dump(exclude={"timezone"}), owner_id=owner_id),
            deadline=deadline,
        )

    def read_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
    ) -> UsageHistory:
        return self._authorized_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            reduce=lambda window: indexed_usage_history(query, window),
        )

    def summary_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
    ) -> IndexedUsageSummary:
        return self._authorized_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            reduce=lambda window: indexed_usage_summary(query, window),
        )

    def read_continuous_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
    ) -> ContinuousUsageHistory:
        return self._authorized_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            reduce=lambda window: continuous_usage_history(
                indexed_usage_history(query, window),
                tuple(
                    UsageRowContext(entry.entry_id, entry.fingerprint, entry.timing_fingerprint)
                    for entry in window.active
                ),
            ),
        )

    def _authorized_read(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"],
        owner_id: UUID | None,
        reduce: Callable[[LedgerWindowRead], _Result],
    ) -> _Result:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
        if mode not in ("mine", "node") or (mode == "mine" and owner_id is not None):
            raise DomainError("VALIDATION_ERROR", "Invalid usage history scope", 422)
        owner_id = actor.actor_id if mode == "mine" else owner_id
        access = self.access_request(actor, query, mode)
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                transaction_budget(
                    session, min(100, self.timeout_ms), min(1000, self.timeout_ms), self.timeout_ms
                )
                self.access.require_in_transaction(session, access)
                result = reduce(
                    self._window_in_transaction(
                        session, query, owner_id=owner_id, deadline=deadline
                    )
                )
                self.access.require_in_transaction(session, access)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "History read budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Usage history database is unavailable",
                503,
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None
