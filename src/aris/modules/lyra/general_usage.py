"""Anonymous recorded totals for general statistics, never individual usage details."""

from time import monotonic
from typing import Literal

from pydantic import AwareDatetime, Field

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.usage_history import UsageHistoryQuery


class GeneralUsageTotal(AccessContract):
    node_id: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False


class GeneralUsageService(IndexedUsageHistoryService):
    def total(self, actor: ActorContext, query: UsageHistoryQuery) -> GeneralUsageTotal:
        query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
        authority = AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_general",
            scope=AccessScope(type="node", id=query.node_id),
        )
        with self.sessions() as session, session.no_autoflush:
            transaction_budget(session, 100, 1000, self.timeout_ms)
            self.access.require_in_transaction(session, authority)
            if query.end_at > self._now():
                raise DomainError("VALIDATION_ERROR", "Usage window has not ended", 422)
            history = self._indexed_in_transaction(
                session,
                query,
                owner_id=None,
                deadline=monotonic() + self.timeout_ms / 1000,
            )
            result = GeneralUsageTotal(
                node_id=history.node_id,
                start_at=history.start_at,
                end_at=history.end_at,
                recorded_microseconds=history.recorded_microseconds,
                gpu_count=history.gpu_count,
            )
            self.access.require_in_transaction(session, authority)
            return result
