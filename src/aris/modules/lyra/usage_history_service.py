"""Scoped read-only history queries; the ledger remains the accounting authority."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, TypeVar
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.continuous_usage import (
    ContinuousUsageHistory,
    UsageRowContext,
    continuous_usage_history,
)
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.ledger_schemas import PostedMeasurement, UsageLedgerEntry, UsageLedgerHead
from aris.modules.lyra.recorded_usage_service import RecordedUsageService
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryQuery, usage_history

_Result = TypeVar("_Result")


class UsageHistoryService(RecordedUsageService):
    def read(
        self,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
    ) -> UsageHistory:
        with self.sessions() as session:
            return self.read_in_transaction(session, actor, query, mode=mode, owner_id=owner_id)

    @staticmethod
    def access_request(
        actor: ActorContext, query: UsageHistoryQuery, mode: Literal["mine", "node"]
    ) -> AccessRequest:
        return AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_own" if mode == "mine" else "usage.read_all",
            scope=AccessScope(type="node", id=query.node_id),
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
        return self._authorized_original_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            reduce=lambda owner, head, entries, at: usage_history(
                query, owner_id=owner, head=head, entries=entries, read_at=at
            ),
        )

    def read_continuous_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
        read_at: datetime | None = None,
    ) -> ContinuousUsageHistory:
        return self._authorized_original_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            data_time=read_at,
            reduce=lambda owner, head, entries, at: continuous_usage_history(
                usage_history(query, owner_id=owner, head=head, entries=entries, read_at=at),
                tuple(
                    UsageRowContext(
                        entry.id, entry.fingerprint, entry.details.measurement.timing_fingerprint
                    )
                    for entry in entries
                    if isinstance(entry.details, PostedMeasurement)
                ),
            ),
        )

    def read_frozen_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        read_at: datetime,
        mode: Literal["mine", "node"] = "mine",
        owner_id: UUID | None = None,
    ) -> UsageHistory:
        if query.snapshot is None:
            raise DomainError("VALIDATION_ERROR", "Frozen history requires its original head", 422)
        return self._authorized_original_read(
            session,
            actor,
            query,
            mode=mode,
            owner_id=owner_id,
            data_time=read_at,
            reduce=lambda owner, head, entries, at: usage_history(
                query, owner_id=owner, head=head, entries=entries, read_at=at
            ),
        )

    def _authorized_original_read(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"],
        owner_id: UUID | None,
        reduce: Callable[
            [UUID | None, UsageLedgerHead, tuple[UsageLedgerEntry, ...], datetime], _Result
        ],
        data_time: datetime | None = None,
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
                transaction_budget(session, 100, 1000, self.timeout_ms)
                self.access.require_in_transaction(session, access)
                now = self._now()
                at = data_time or now
                if at.tzinfo is None or at.utcoffset() is None:
                    raise DomainError(
                        "VALIDATION_ERROR", "History data time requires timezone", 422
                    )
                at = at.astimezone(UTC)
                if query.end_at > at or at > now:
                    raise DomainError("VALIDATION_ERROR", "Usage window has not ended", 422)
                repo = UsageLedgerRepository(session)
                current = repo.head(query.node_id)
                head = query.snapshot or current
                if head.revision > current.revision:
                    raise DomainError("CONFLICT", "Usage snapshot has not been committed", 409)
                if data_time is not None and head.revision:
                    point = repo.revision(query.node_id, head.revision)
                    if point is None or point.fingerprint != head.fingerprint:
                        raise DomainError("CONFLICT", "History snapshot differs", 409)
                    if point.recorded_at > at:
                        raise DomainError(
                            "VALIDATION_ERROR", "History head exceeds its data time", 422
                        )
                entries = repo.prefix(head)
                result = reduce(
                    owner_id, head, entries, at if data_time is not None else self._now()
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
