"""No partial-success group totals: every selected node is currently authorized and readable."""

from time import monotonic
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.group_usage import GroupUsageQuery, GroupUsageReport, group_usage_report
from aris.modules.lyra.ledger_schemas import canonical_ledger
from aris.modules.lyra.recorded_usage import MAX_REPORT_BYTES, MAX_REPORT_ENTRIES
from aris.modules.lyra.usage_history import UsageHistoryQuery
from aris.modules.lyra.usage_history_service import UsageHistoryService


class GroupUsageService:
    def __init__(self, history: UsageHistoryService, *, timeout_ms: int = 8000) -> None:
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 10000:
            raise ValueError("group usage requires a bounded timeout")
        self.history, self.timeout_ms = history, timeout_ms

    def require_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: GroupUsageQuery,
        mode: Literal["mine", "node"],
    ) -> None:
        if mode not in ("mine", "node"):
            raise DomainError("VALIDATION_ERROR", "Invalid group usage view", 422)
        for node in query.node_ids:
            selection = UsageHistoryQuery(
                node_id=node, start_at=query.start_at, end_at=query.end_at, timezone=query.timezone
            )
            self.history.access.require_in_transaction(
                session, self.history.access_request(actor, selection, mode)
            )

    def read(
        self, actor: ActorContext, query: GroupUsageQuery, *, mode: Literal["mine", "node"] = "mine"
    ) -> GroupUsageReport:
        with self.history.sessions() as session:
            return self.read_in_transaction(session, actor, query, mode=mode)

    def read_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: GroupUsageQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
    ) -> GroupUsageReport:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = GroupUsageQuery.model_validate(query.model_dump(mode="json"))
        if mode not in ("mine", "node"):
            raise DomainError("VALIDATION_ERROR", "Invalid group usage view", 422)
        deadline = monotonic() + self.timeout_ms / 1000
        start = self.history._now()
        try:
            transaction_budget(session, 100, 1000, self.timeout_ms)
            histories = []
            size = rows = 0
            heads = {head.node_id: head for head in query.snapshots or ()}
            for node in query.node_ids:
                value = self.history.read_in_transaction(
                    session,
                    actor,
                    UsageHistoryQuery(
                        node_id=node,
                        start_at=query.start_at,
                        end_at=query.end_at,
                        timezone=query.timezone,
                        snapshot=heads.get(node),
                    ),
                    mode=mode,
                )
                size += len(canonical_ledger(value).encode())
                rows += len(value.rows)
                if size > MAX_REPORT_BYTES or rows > MAX_REPORT_ENTRIES:
                    raise DomainError(
                        "USAGE_REPORT_TOO_LARGE", "Selected-node report exceeds its budget", 503
                    )
                histories.append(value)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Group usage budget exceeded", 503
                    )
            result = group_usage_report(
                query,
                tuple(histories),
                owner_id=actor.actor_id if mode == "mine" else None,
                started_at=start,
                finished_at=self.history._now(),
            )
            if len(canonical_ledger(result).encode()) > MAX_REPORT_BYTES:
                raise DomainError(
                    "USAGE_REPORT_TOO_LARGE", "Selected-node report exceeds its budget", 503
                )
            self.require_in_transaction(session, actor, query, mode)
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Group usage budget exceeded", 503)
            return result
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Selected-node usage is unavailable", 503
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Selected-node usage is inconsistent", 503
            ) from None
