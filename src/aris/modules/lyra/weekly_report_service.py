"""Registered transaction-local report generation and separately authorized reads."""

from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import UserLabelQuery
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.scheduler.schemas import ScheduledTask
from aris.core.scheduler.service import TaskDefinition
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryQuery
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_repo import WeeklyReportRepository
from aris.modules.lyra.weekly_report_schemas import (
    StoredWeeklyReport,
    WeeklyReportCursor,
    WeeklyReportPage,
    WeeklyReportRequest,
    weekly_recorded_summary,
)

WEEKLY_KINDS = {mode: "lyra.weekly_recorded_usage." + mode for mode in ("mine", "node")}


class WeeklyReportHandler:
    def __init__(self, history: UsageHistoryService) -> None:
        self.history = history

    @staticmethod
    def definitions() -> tuple[TaskDefinition, ...]:
        return tuple(
            TaskDefinition(kind=WEEKLY_KINDS[mode], permission=permission, scope_type="node")
            for mode, permission in (("mine", "usage.read_own"), ("node", "usage.read_all"))
        )

    def validate(self, request: ScheduledTask) -> None:
        try:
            payload = WeeklyReportRequest.model_validate(request.payload)
            if (
                request.scope.type != "node"
                or request.kind != WEEKLY_KINDS[payload.mode]
                or request.scheduled_for < payload.end_at
            ):
                raise ValueError("weekly task differs from its period and scope")
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "Invalid weekly report task", 422) from None

    def execute_in_transaction(
        self, session: Session, request: ScheduledTask, deadline: float
    ) -> dict[str, JsonValue]:
        self.validate(request)
        payload = WeeklyReportRequest.model_validate(request.payload)
        actor = ActorContext(
            actor_id=request.owner_id, request_id="weekly-report-" + str(request.id)
        )
        query = UsageHistoryQuery(
            node_id=request.scope.id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            timezone=payload.timezone,
        )
        access = self.history.access_request(actor, query, payload.mode)
        self.history.access.require_in_transaction(session, access)
        repo = WeeklyReportRepository(session)
        previous = repo.get(request.id)
        if previous is not None:
            if previous.task_fingerprint != request.fingerprint:
                raise DomainError("CONFLICT", "Weekly report has a different task", 409)
            return {"report_id": str(previous.id), "fingerprint": previous.fingerprint}
        report = self.history.read_in_transaction(session, actor, query, mode=payload.mode)
        owner_ids = {value.owner_id for value in report.owners}
        if report.owner_id is not None:
            owner_ids.add(report.owner_id)
        labels = {
            str(value.user_id): value.display_name
            for value in self.history.access.labels_in_transaction(
                session, UserLabelQuery(user_ids=tuple(sorted(owner_ids, key=str)))
            )
        }
        now, event_id = self.history._now(), uuid4()
        value = StoredWeeklyReport(
            id=request.id,
            event_id=event_id,
            requested_by=request.owner_id,
            task_fingerprint=request.fingerprint,
            request=payload,
            created_at=now,
            history=weekly_recorded_summary(report),
            labels=labels,
            fingerprint="0" * 64,
        )
        value = value.model_copy(update={"fingerprint": value.content_fingerprint()})
        summary = {
            "report_id": str(value.id),
            "node_id": report.node_id,
            "assessment_scope": "recorded_usage_only",
            "fingerprint": value.fingerprint,
        }
        session.add(
            DomainEvent(
                event_id=str(event_id),
                event_type="usage.weekly_report.created",
                event_version=1,
                aggregate_type="usage_weekly_report",
                aggregate_id=str(value.id),
                aggregate_version=1,
                occurred_at=now,
                recorded_at=now,
                actor="system:scheduler",
                correlation_id=actor.request_id,
                causation_id=str(request.id),
                payload=summary,
            )
        )
        session.flush()
        repo.append(value)
        session.add(OutboxEntry(event_id=str(event_id), created_at=now))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor="system:scheduler",
                action="usage.weekly_report.created",
                target=str(value.id),
                request_id=actor.request_id,
                recorded_at=now,
                changes=summary,
            )
        )
        self.history.access.require_in_transaction(session, access)
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Weekly report budget exceeded", 503)
        return {"report_id": str(value.id), "fingerprint": value.fingerprint}


class WeeklyReportReader:
    """Reads existing artifacts only; constructing a reader cannot schedule reports."""

    def __init__(self, history: UsageHistoryService) -> None:
        self.history = history

    def require_in_transaction(
        self, session: Session, actor: ActorContext, node_id: str, mode: Literal["mine", "node"]
    ) -> None:
        from aris.core.access.schemas import AccessRequest, AccessScope

        if mode not in ("mine", "node"):
            raise DomainError("VALIDATION_ERROR", "Invalid weekly report view", 422)
        self.history.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=actor.actor_id,
                permission="usage.read_own" if mode == "mine" else "usage.read_all",
                scope=AccessScope(type="node", id=node_id),
            ),
        )

    def list_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: UsageHistoryQuery,
        *,
        mode: Literal["mine", "node"] = "mine",
        before: WeeklyReportCursor | None = None,
        limit: int = 16,
    ) -> WeeklyReportPage:
        if mode not in ("mine", "node") or type(limit) is not int or not 1 <= limit <= 32:
            raise DomainError("VALIDATION_ERROR", "Invalid weekly report selection", 422)
        query = UsageHistoryQuery.model_validate(query.model_dump(mode="json"))
        access = self.history.access_request(actor, query, mode)
        self.history.access.require_in_transaction(session, access)
        values = WeeklyReportRepository(session).summaries(
            node_id=query.node_id,
            owner_id=actor.actor_id if mode == "mine" else None,
            start_at=query.start_at,
            end_at=query.end_at,
            before=before,
            limit=limit,
        )
        items = values[:limit]
        cursor = (
            WeeklyReportCursor(created_at=items[-1].created_at, id=items[-1].id)
            if len(values) > limit
            else None
        )
        self.history.access.require_in_transaction(session, access)
        return WeeklyReportPage(items=items, next_cursor=cursor)

    def read_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        id: UUID,
        *,
        node_id: str,
        mode: Literal["mine", "node"] = "mine",
    ) -> StoredWeeklyReport:
        from aris.core.access.schemas import AccessRequest, AccessScope

        if mode not in ("mine", "node"):
            raise DomainError("VALIDATION_ERROR", "Invalid weekly report view", 422)
        access = AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_own" if mode == "mine" else "usage.read_all",
            scope=AccessScope(type="node", id=node_id),
        )
        self.history.access.require_in_transaction(session, access)
        result = WeeklyReportRepository(session).get(id)
        if result is None or result.history.node_id != node_id:
            raise DomainError("NOT_FOUND", "Weekly report not found", 404)
        if result.request.mode != mode or (
            mode == "mine" and result.history.owner_id != actor.actor_id
        ):
            raise DomainError("PERMISSION_DENIED", "Weekly report is outside this view", 403)
        self.history.access.require_in_transaction(session, access)
        return result

    def read(
        self, actor: ActorContext, id: UUID, *, node_id: str, mode: Literal["mine", "node"] = "mine"
    ) -> StoredWeeklyReport:
        try:
            with self.history.sessions() as session:
                transaction_budget(session, 100, 1000, self.history.timeout_ms)
                return self.read_in_transaction(session, actor, id, node_id=node_id, mode=mode)
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Weekly report database is unavailable", 503
            ) from None

    def materialize_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        id: UUID,
        *,
        node_id: str,
        mode: Literal["mine", "node"] = "mine",
    ) -> tuple[StoredWeeklyReport, UsageHistory]:
        saved = self.read_in_transaction(session, actor, id, node_id=node_id, mode=mode)
        original = saved.history
        history = self.history.read_frozen_in_transaction(
            session,
            actor,
            UsageHistoryQuery(
                node_id=node_id,
                start_at=original.start_at,
                end_at=original.end_at,
                timezone=original.timezone,
                snapshot=original.snapshot,
            ),
            read_at=original.read_at,
            mode=mode,
        )
        if weekly_recorded_summary(history) != original:
            raise DomainError("CONFLICT", "Saved weekly source no longer matches its summary", 409)
        self.require_in_transaction(session, actor, node_id, mode)
        return saved, history
