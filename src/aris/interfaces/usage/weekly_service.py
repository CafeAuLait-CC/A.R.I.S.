"""Authenticated existing-report reads and downloads; never schedules or generates."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract
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
from aris.modules.lyra.usage_history import UsageHistoryQuery, calendar_boundaries
from aris.modules.lyra.weekly_report_schemas import (
    StoredWeeklyReport,
    WeeklyReportCursor,
    WeeklyReportPage,
)


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


def weekly_usage(
    surface: UsageHistorySurface,
    authorization: str | None,
    request_id: str,
    query: WeeklyUsageQuery | WeeklyUsageRead,
    *,
    output: Literal["json", "html", "csv"] = "json",
) -> WeeklyReportPage | StoredWeeklyReport | bytes:
    subject = surface.authenticator.authenticate(authorization)
    if query.node_id not in surface.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    reader = surface.weekly_reports
    if reader is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Weekly report reading is not enabled", 503)
    try:
        with surface.history.sessions() as session:
            transaction_budget(session, 100, 1000, surface.history.timeout_ms)
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            result: WeeklyReportPage | StoredWeeklyReport | bytes
            if isinstance(query, WeeklyUsageRead):
                report = reader.read_in_transaction(
                    session, actor, query.id, node_id=query.node_id, mode=query.mode
                )
                result = (
                    history_report(report.history, report.labels)
                    if output == "html"
                    else history_csv(report.history, report.labels)
                    if output == "csv"
                    else report
                )
                # Export rendering must not bypass a concurrent current permission change.
                reader.require_in_transaction(session, actor, query.node_id, query.mode)
            else:
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
