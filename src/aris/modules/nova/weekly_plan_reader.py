"""Current scoped views of saved plans and Core task metadata; no scheduling capability."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.scheduler.schemas import TaskQueueQuery
from aris.core.scheduler.service import SchedulerService
from aris.modules.lyra.weekly_report_service import WEEKLY_KINDS
from aris.modules.nova.weekly_plan_repo import WeeklyPlanRepository
from aris.modules.nova.weekly_plan_schemas import WeeklyPlanPage, WeeklyPlanView, plan_id, plan_key


class WeeklyPlanReader:
    def __init__(
        self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)
        self.queue = SchedulerService(sessions)

    def read_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        *,
        node_id: str,
        mode: Literal["mine", "node"] = "mine",
        limit: int = 16,
    ) -> WeeklyPlanPage:
        if mode not in ("mine", "node") or type(limit) is not int or not 1 <= limit <= 16:
            raise DomainError("VALIDATION_ERROR", "Invalid weekly plan view", 422)
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        access = AccessRequest(
            user_id=actor.actor_id,
            scope=AccessScope(type="node", id=node_id),
            permission="usage.read_own" if mode == "mine" else "usage.read_all",
        )
        deadline = monotonic() + 5
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("weekly status clock requires a timezone")
        now = now.astimezone(UTC)
        try:
            transaction_budget(session, 100, 1000, 5000)
            self.access.require_in_transaction(session, access)
            values = WeeklyPlanRepository(session).for_view(node_id, actor.actor_id, mode, limit)
            items = []
            for value in values[:limit]:
                plan = value.plan
                tasks = self.queue.queue_in_transaction(
                    session,
                    TaskQueueQuery(
                        kind=WEEKLY_KINDS[mode],
                        owner_id=plan.owner_id,
                        scope=plan.scope,
                        key_prefix=plan_key(plan) + "/",
                    ),
                    now=now,
                )
                items.append(
                    WeeklyPlanView(
                        id=plan_id(plan),
                        name=plan.namespace,
                        node_id=node_id,
                        report_type=plan.report_type,
                        timezone=plan.timezone,
                        first_week=plan.first_week,
                        next_week=value.next_week,
                        next_due_at=value.next_due_at,
                        last_prepared_at=value.last_prepared_at,
                        last_error_code=value.last_error_code,
                        retry_at=value.retry_at,
                        jobs=tasks,
                    )
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Weekly status budget exceeded", 503
                    )
            self.access.require_in_transaction(session, access)
            return WeeklyPlanPage(items=tuple(items), has_more=len(values) > limit, read_at=now)
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Weekly plan status is unavailable", 503
            ) from None
