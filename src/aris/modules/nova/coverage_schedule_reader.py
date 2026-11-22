"""Read configured plan identities only; no scheduling or worker-health authority."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.nova.coverage_schedule_repo import CoverageScheduleRepository
from aris.modules.nova.coverage_schedule_schemas import DailyCoveragePeriodResult
from aris.modules.nova.report_calendar import weekly_report_period


class CoveragePlanResultView(AccessContract):
    week: date
    snapshot_id: UUID | None
    report_id: UUID | None = None
    coverage: Literal["complete", "partial", "unknown"]
    saved_at: AwareDatetime


class CoveragePlanView(AccessContract):
    id: UUID
    node_id: str
    timezone: str
    next_week: date
    next_due_at: AwareDatetime
    state: Literal["scheduled", "pending", "delayed"]
    last_result: CoveragePlanResultView | None
    worker_health: Literal["not_assessed"] = "not_assessed"


class CoveragePlanPage(AccessContract):
    items: tuple[CoveragePlanView, ...]
    read_at: AwareDatetime


class CoverageScheduleReader:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        plan_ids: tuple[UUID, ...],
        *,
        clock: Callable[[], datetime] | None = None,
        include_node_for_personal: bool = False,
    ) -> None:
        if not 1 <= len(plan_ids) <= 8 or len(set(plan_ids)) != len(plan_ids):
            raise ValueError("coverage status requires at most eight unique configured IDs")
        self.sessions, self.plan_ids = sessions, plan_ids
        self.clock = clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)
        self.include_node_for_personal = include_node_for_personal

    def read_in_transaction(
        self, session: Session, actor: ActorContext, *, node_id: str, mode: Literal["mine", "node"]
    ) -> CoveragePlanPage:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        if mode not in ("mine", "node"):
            raise DomainError("VALIDATION_ERROR", "Invalid coverage plan view", 422)
        request = AccessRequest(
            user_id=actor.actor_id,
            scope=AccessScope(type="node", id=node_id),
            permission="usage.read_own" if mode == "mine" else "usage.read_all",
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("coverage status clock requires timezone")
        now = now.astimezone(UTC)
        deadline = monotonic() + 3
        try:
            transaction_budget(session, 100, 1000, 3000)
            self.access.require_in_transaction(session, request)
            repo = CoverageScheduleRepository(session)
            items = []
            for id in self.plan_ids:
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Coverage status budget exceeded", 503
                    )
                status = repo.get(id)
                if status is None:
                    continue
                plan = status.definition.calendar
                own = plan.report_type == "usage.mine" and plan.owner_id == actor.actor_id
                node = plan.report_type == "usage.node"
                if plan.scope.id != node_id:
                    continue
                if mode == "node" and not node:
                    continue
                if mode == "mine" and not (own or (self.include_node_for_personal and node)):
                    continue
                due = weekly_report_period(plan, status.next_week).scheduled_for
                prior = repo.last_result(status)
                last_result = None
                if isinstance(prior, DailyCoveragePeriodResult):
                    last_result = CoveragePlanResultView(
                        week=prior.week,
                        snapshot_id=None,
                        report_id=prior.id,
                        coverage=prior.report.coverage,
                        saved_at=prior.created_at,
                    )
                elif prior is not None:
                    last_result = CoveragePlanResultView(
                        week=prior.week,
                        snapshot_id=prior.snapshot_id,
                        coverage=prior.coverage,
                        saved_at=prior.created_at,
                    )
                items.append(
                    CoveragePlanView(
                        id=id,
                        node_id=node_id,
                        timezone=plan.timezone,
                        next_week=status.next_week,
                        next_due_at=due,
                        state="delayed"
                        if now >= due + timedelta(minutes=30)
                        else "pending"
                        if now >= due
                        else "scheduled",
                        last_result=last_result,
                    )
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Coverage status budget exceeded", 503
                    )
            self.access.require_in_transaction(session, request)
            return CoveragePlanPage(items=tuple(items), read_at=now)
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Coverage plan status is unavailable", 503
            ) from None
