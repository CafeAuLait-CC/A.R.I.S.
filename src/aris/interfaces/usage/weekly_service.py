"""Authenticated existing-report reads and downloads; never schedules or generates."""

from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract, UserLabelQuery
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.usage.report import history_report
from aris.interfaces.usage.service import (
    CalendarWindow,
    UsageHistorySurface,
    UTCWindow,
    history_csv,
)
from aris.interfaces.usage.weekly_daily_export import weekly_daily_csv, weekly_daily_html
from aris.modules.lyra.ledger_window_read import MAX_LEDGER_WINDOW_INDEX_BYTES
from aris.modules.lyra.usage_history import UsageHistoryQuery, calendar_boundaries
from aris.modules.lyra.weekly_report_schemas import (
    StoredWeeklyReport,
    WeeklyReportCursor,
    WeeklyReportPage,
)
from aris.modules.nova.coverage_schedule_reader import WeeklyCoverageReportView
from aris.modules.nova.weekly_plan_schemas import WeeklyPlanPage
from aris.modules.vega.target_schemas import GpuLabelQuery


class WeeklyUsageQuery(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]
    before: WeeklyReportCursor | None = None
    limit: int = Field(default=16, ge=1, le=32, strict=True)


class WeeklyUsageRead(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"
    id: UUID
    source: Literal["recorded", "daily_rollups"] = "recorded"


class WeeklyPlanRead(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"


def weekly_plans(
    surface: UsageHistorySurface, authorization: str | None, request_id: str, query: WeeklyPlanRead
) -> WeeklyPlanPage:
    subject = surface.authenticator.authenticate(authorization)
    if query.node_id not in surface.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    reader = surface.weekly_plans
    if reader is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Weekly plan reading is not enabled", 503)
    try:
        with surface.history.sessions() as session:
            transaction_budget(session, 100, 1000, 5000)
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            result = reader.read_in_transaction(
                session, actor, node_id=query.node_id, mode=query.mode
            )
            current = surface.history.access.resolve_external_in_transaction(session, subject)
            if current is None or current.user_id != principal.user_id:
                raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
            return result
    except DBAPIError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Weekly plan status is unavailable", 503
        ) from None


def _daily_report(
    surface: UsageHistorySurface,
    session: Session,
    actor: ActorContext,
    query: WeeklyUsageRead,
    output: Literal["json", "html", "csv"],
) -> WeeklyCoverageReportView | bytes:
    reader = surface.coverage_plans
    if reader is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Coverage plan reading is not enabled", 503)
    deadline = monotonic() + surface.history.timeout_ms / 1000
    view = reader.report_in_transaction(
        session, actor, query.id, node_id=query.node_id, mode=query.mode
    )
    result: WeeklyCoverageReportView | bytes = view
    if output != "json":
        owners = tuple(sorted({row.owner_id for row in view.durations if row.owner_id}, key=str))
        labels = {}
        for offset in range(0, len(owners), 128):
            labels.update(
                {
                    str(label.user_id): label.display_name
                    for label in surface.history.access.labels_in_transaction(
                        session, UserLabelQuery(user_ids=owners[offset : offset + 128])
                    )
                }
            )
        gpu_names = {}
        if surface.gpu_labels is not None:
            uuids = tuple(sorted({row.gpu_uuid for row in view.durations}))
            for offset in range(0, len(uuids), 100):
                gpu_names.update(
                    {
                        label.gpu_uuid: f"GPU {label.index}"
                        for label in surface.gpu_labels.labels_in_transaction(
                            session, GpuLabelQuery(gpu_uuids=uuids[offset : offset + 100])
                        )
                        if label.node_id == query.node_id
                    }
                )
        render = weekly_daily_html if output == "html" else weekly_daily_csv
        result = render(view, labels, gpu_names)
        if len(result) > MAX_LEDGER_WINDOW_INDEX_BYTES:
            raise DomainError("USAGE_REPORT_TOO_LARGE", "Choose a smaller weekly export", 503)
    reader.require_report_in_transaction(session, actor, query.node_id, query.mode)
    if monotonic() >= deadline:
        raise DomainError("RETRYABLE_DATABASE_ERROR", "Weekly export budget exceeded", 503)
    return result


def weekly_usage(
    surface: UsageHistorySurface,
    authorization: str | None,
    request_id: str,
    query: WeeklyUsageQuery | WeeklyUsageRead,
    *,
    output: Literal["json", "html", "csv"] = "json",
) -> WeeklyReportPage | StoredWeeklyReport | WeeklyCoverageReportView | bytes:
    subject = surface.authenticator.authenticate(authorization)
    if query.node_id not in surface.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    reader = surface.weekly_reports
    daily = isinstance(query, WeeklyUsageRead) and query.source == "daily_rollups"
    if reader is None and not daily:
        raise DomainError("USAGE_NOT_CONFIGURED", "Weekly report reading is not enabled", 503)
    try:
        with surface.history.sessions() as session:
            transaction_budget(session, 100, 1000, surface.history.timeout_ms)
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            result: WeeklyReportPage | StoredWeeklyReport | WeeklyCoverageReportView | bytes
            if daily and isinstance(query, WeeklyUsageRead):
                result = _daily_report(surface, session, actor, query, output)
            elif isinstance(query, WeeklyUsageRead):
                assert reader is not None
                if output == "json":
                    result = reader.read_in_transaction(
                        session, actor, query.id, node_id=query.node_id, mode=query.mode
                    )
                else:
                    report, details = reader.materialize_in_transaction(
                        session, actor, query.id, node_id=query.node_id, mode=query.mode
                    )
                    result = (
                        history_report(details, report.labels)
                        if output == "html"
                        else history_csv(details, report.labels)
                    )
                    if len(result) > MAX_LEDGER_WINDOW_INDEX_BYTES:
                        raise DomainError(
                            "USAGE_REPORT_TOO_LARGE", "Choose a smaller weekly export", 503
                        )
                # Export rendering must not bypass a concurrent current permission change.
                reader.require_in_transaction(session, actor, query.node_id, query.mode)
            else:
                assert reader is not None
                if output != "json":
                    raise DomainError("VALIDATION_ERROR", "Export requires an existing report", 422)
                if isinstance(query.window, CalendarWindow):
                    boundaries = calendar_boundaries(
                        query.window.start_date, query.window.end_date, query.window.timezone
                    )
                    start, end = boundaries[0], min(boundaries[-1], surface.history._now())
                else:
                    start, end = query.window.start_at, query.window.end_at
                try:
                    selection = UsageHistoryQuery(
                        node_id=query.node_id,
                        start_at=start,
                        end_at=end,
                        timezone=query.window.timezone,
                    )
                except ValueError:
                    raise DomainError(
                        "VALIDATION_ERROR", "Invalid weekly report date range", 422
                    ) from None
                result = reader.list_in_transaction(
                    session,
                    actor,
                    selection,
                    mode=query.mode,
                    before=query.before,
                    limit=query.limit,
                )
            current = surface.history.access.resolve_external_in_transaction(session, subject)
            if current is None or current.user_id != principal.user_id:
                raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
            return result
    except DBAPIError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Weekly report database is unavailable", 503
        ) from None
