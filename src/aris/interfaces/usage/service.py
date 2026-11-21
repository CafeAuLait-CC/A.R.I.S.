"""Authenticated read-only usage presentation; no command or ledger write capability."""

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import (
    AccessContract,
    AccessRequest,
    AccessScope,
    UserLabelQuery,
    UserPrincipal,
)
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserCommandAuthenticator
from aris.interfaces.usage.activity_service import ActivityPreviewAccess
from aris.interfaces.usage.report import history_report
from aris.modules.lyra.continuous_usage import ContinuousUsageHistory, ContinuousUsageInterval
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.ledger_window_read import (
    MAX_LEDGER_WINDOW_ENTRIES,
    MAX_LEDGER_WINDOW_INDEX_BYTES,
)
from aris.modules.lyra.usage_history import (
    UsageHistory,
    UsageHistoryQuery,
    calendar_boundaries,
)
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_service import WeeklyReportReader
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.coverage_schedule_reader import CoverageScheduleReader
from aris.modules.nova.weekly_plan_reader import WeeklyPlanReader


class CalendarWindow(AccessContract):
    kind: Literal["calendar"] = "calendar"
    start_date: date
    end_date: date
    timezone: str = Field(min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        return UserPrincipal.iana_timezone(value)

    @model_validator(mode="after")
    def date_range(self) -> "CalendarWindow":
        if not 0 <= (self.end_date - self.start_date).days <= 365:
            raise ValueError("select at most 366 calendar dates")
        return self


class UTCWindow(AccessContract):
    kind: Literal["utc"] = "utc"
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=128)


class UsageHistoryInput(AccessContract):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    mode: Literal["mine", "node"] = "mine"
    owner_id: UUID | None = None
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]
    snapshot: UsageLedgerHead | None = None
    offset: int = Field(default=0, ge=0, le=MAX_LEDGER_WINDOW_ENTRIES, strict=True)
    limit: int = Field(default=25, ge=1, le=100, strict=True)
    presentation: Literal["segments", "continuous"] = "segments"

    @model_validator(mode="after")
    def fixed_pages(self) -> "UsageHistoryInput":
        if (self.mode == "mine" and self.owner_id is not None) or (
            self.offset > 0 and (self.snapshot is None or self.window.kind != "utc")
        ):
            raise ValueError(
                "owner filters require node view; later pages require a fixed snapshot"
            )
        if self.snapshot is not None and (
            self.window.kind != "utc" or self.snapshot.node_id != self.node_id
        ):
            raise ValueError("snapshots require matching UTC windows and node")
        return self


class UsageNodeAccess(AccessContract):
    node_id: str
    mine: bool
    node: bool


class UsageSession(AccessContract):
    actor: UserPrincipal
    nodes: tuple[UsageNodeAccess, ...]
    credential_mode: Literal["development_bound_user", "member_session"] = "development_bound_user"
    saved_snapshots_available: bool = False
    weekly_reports_available: bool = False
    weekly_plans_available: bool = False
    coverage_plans_available: bool = False
    group_reports_available: bool = False
    activity_preview_nodes: tuple[str, ...] = ()


class UsageHistoryPage(AccessContract):
    report: UsageHistory
    labels: dict[str, str]
    offset: int
    limit: int
    has_more: bool
    presentation: Literal["segments", "continuous"] = "segments"
    interval_count: int | None = None
    intervals: tuple[ContinuousUsageInterval, ...] = ()


def csv_text(value: str) -> str:
    # Spreadsheet applications can interpret quoted cell text as a formula.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def history_csv(report: UsageHistory, labels: dict[str, str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "node",
            "user_id",
            "display_name",
            "gpu_uuid",
            "gpu_model",
            "start_utc",
            "end_utc",
            "display_timezone",
            "recorded_gpu_hours",
            "recorded_microseconds",
            "source_derivation_id",
            "ledger_entry_id",
            "entry_fingerprint",
            "report_fingerprint",
            "source_coverage",
            "quota_usage_available",
        )
    )
    for row in report.rows:
        writer.writerow(
            (
                csv_text(report.node_id),
                str(row.owner_id),
                csv_text(labels.get(str(row.owner_id), str(row.owner_id))),
                csv_text(row.gpu_uuid),
                csv_text(row.model),
                row.start_at.isoformat(),
                row.end_at.isoformat(),
                report.timezone,
                str(
                    (Decimal(row.recorded_microseconds) / Decimal(3600000000)).quantize(
                        Decimal("0.000000000")
                    )
                ),
                row.recorded_microseconds,
                str(row.source_derivation_id),
                str(row.entry_id),
                row.entry_fingerprint,
                report.fingerprint,
                "unknown",
                "false",
            )
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def continuous_history_csv(view: ContinuousUsageHistory, labels: dict[str, str]) -> bytes:
    report = view.history
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "node",
            "user_id",
            "display_name",
            "gpu_uuid",
            "gpu_model",
            "start_utc",
            "end_utc",
            "display_timezone",
            "recorded_gpu_hours",
            "recorded_microseconds",
            "source_segment_count",
            "first_ledger_entry_id",
            "last_ledger_entry_id",
            "interval_fingerprint",
            "report_fingerprint",
            "source_coverage",
            "quota_usage_available",
        )
    )
    for row in view.intervals:
        writer.writerow(
            (
                csv_text(report.node_id),
                str(row.owner_id),
                csv_text(labels.get(str(row.owner_id), str(row.owner_id))),
                csv_text(row.gpu_uuid),
                csv_text(row.model),
                row.start_at.isoformat(),
                row.end_at.isoformat(),
                report.timezone,
                str(
                    (Decimal(row.recorded_microseconds) / Decimal(3600000000)).quantize(
                        Decimal("0.000000000")
                    )
                ),
                row.recorded_microseconds,
                row.source_segment_count,
                str(row.first_entry_id),
                str(row.last_entry_id),
                row.fingerprint,
                report.fingerprint,
                "unknown",
                "false",
            )
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


class UsageHistorySurface:
    def __init__(
        self,
        history: UsageHistoryService,
        authenticator: UserCommandAuthenticator,
        node_ids: tuple[str, ...],
        *,
        saved_windows: UsageWindowReader | None = None,
        weekly_reports: WeeklyReportReader | None = None,
        weekly_plans: WeeklyPlanReader | None = None,
        coverage_plans: CoverageScheduleReader | None = None,
        activity: ActivityPreviewAccess | None = None,
    ) -> None:
        if not 1 <= len(node_ids) <= 32 or len(set(node_ids)) != len(node_ids):
            raise ValueError("usage history requires explicit unique nodes")
        for node in node_ids:
            AccessScope(type="node", id=node)
        self.history, self.authenticator, self.node_ids = history, authenticator, node_ids
        self.saved_windows = saved_windows
        self.weekly_reports = weekly_reports
        self.weekly_plans = weekly_plans
        self.coverage_plans = coverage_plans
        if activity is not None and (
            activity.reader.sessions is not history.sessions
            or any(node not in node_ids for node in activity.node_ids)
        ):
            raise ValueError("activity preview requires the same database and allowed nodes")
        self.activity = activity

    def session(self, authorization: str | None) -> UsageSession:
        subject = self.authenticator.authenticate(authorization)
        try:
            with self.history.sessions() as session:
                transaction_budget(session, 100, 1000, self.history.timeout_ms)
                principal = self.history.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
                nodes = []
                for node in self.node_ids:
                    grants = []
                    for permission in ("usage.read_own", "usage.read_all"):
                        try:
                            self.history.access.require_in_transaction(
                                session,
                                AccessRequest(
                                    user_id=principal.user_id,
                                    permission=permission,
                                    scope=AccessScope(type="node", id=node),
                                ),
                            )
                            grants.append(True)
                        except DomainError as error:
                            if error.code != "PERMISSION_DENIED":
                                raise
                            grants.append(False)
                    if any(grants):
                        nodes.append(UsageNodeAccess(node_id=node, mine=grants[0], node=grants[1]))
                if not nodes:
                    raise DomainError("PERMISSION_DENIED", "Usage read permission is required", 403)
                return UsageSession(
                    actor=principal,
                    credential_mode="member_session"
                    if getattr(self.authenticator, "browser_sessions", False)
                    else "development_bound_user",
                    nodes=tuple(nodes),
                    saved_snapshots_available=self.saved_windows is not None,
                    weekly_reports_available=self.weekly_reports is not None,
                    weekly_plans_available=self.weekly_plans is not None,
                    coverage_plans_available=self.coverage_plans is not None,
                    group_reports_available=len(nodes) > 1,
                    activity_preview_nodes=tuple(
                        node.node_id
                        for node in nodes
                        if node.node
                        and self.activity is not None
                        and node.node_id in self.activity.node_ids
                    ),
                )
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Identity database is unavailable", 503
            ) from None

    def read(
        self,
        authorization: str | None,
        request_id: str,
        query: UsageHistoryInput,
        *,
        output: Literal["page", "csv", "html"] = "page",
    ) -> UsageHistoryPage | bytes:
        subject = self.authenticator.authenticate(authorization)
        query = UsageHistoryInput.model_validate(query.model_dump(mode="json"))
        if query.node_id not in self.node_ids:
            raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
        if output not in ("page", "csv", "html"):
            raise DomainError("VALIDATION_ERROR", "Usage output format is invalid", 422)
        if output != "page" and (query.snapshot is None or query.window.kind != "utc"):
            raise DomainError("VALIDATION_ERROR", "Export requires the displayed snapshot", 422)
        if isinstance(query.window, CalendarWindow):
            bounds = calendar_boundaries(
                query.window.start_date, query.window.end_date, query.window.timezone
            )
            start, end = bounds[0], min(bounds[-1], self.history._now())
            if start >= end:
                raise DomainError("VALIDATION_ERROR", "Select a date range that has started", 422)
        else:
            start, end = query.window.start_at, query.window.end_at
        try:
            selection = UsageHistoryQuery(
                node_id=query.node_id,
                start_at=start,
                end_at=end,
                timezone=query.window.timezone,
                snapshot=query.snapshot,
            )
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "Usage date range is invalid", 422) from None
        try:
            with self.history.sessions() as session:
                transaction_budget(session, 100, 1000, self.history.timeout_ms)
                principal = self.history.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
                actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
                continuous = (
                    self.history.read_continuous_in_transaction(
                        session, actor, selection, mode=query.mode, owner_id=query.owner_id
                    )
                    if query.presentation == "continuous"
                    else None
                )
                report = (
                    continuous.history
                    if continuous is not None
                    else self.history.read_in_transaction(
                        session, actor, selection, mode=query.mode, owner_id=query.owner_id
                    )
                )
                labels = {
                    str(label.user_id): label.display_name
                    for label in self.history.access.labels_in_transaction(
                        session,
                        UserLabelQuery(user_ids=tuple(owner.owner_id for owner in report.owners)),
                    )
                }
                result = (
                    (
                        (
                            continuous_history_csv(continuous, labels)
                            if continuous is not None
                            else history_csv(report, labels)
                        )
                        if output == "csv"
                        else (
                            history_report(
                                report, labels, continuous_interval_count=len(continuous.intervals)
                            )
                            if continuous is not None
                            else history_report(report, labels)
                        )
                    )
                    if output != "page"
                    else UsageHistoryPage(
                        report=report.model_copy(
                            update={
                                "rows": ()
                                if continuous is not None
                                else report.rows[query.offset : query.offset + query.limit]
                            }
                        ),
                        labels=labels,
                        offset=query.offset,
                        limit=query.limit,
                        has_more=query.offset + query.limit
                        < (
                            len(continuous.intervals)
                            if continuous is not None
                            else report.segment_count
                        ),
                        presentation=query.presentation,
                        interval_count=len(continuous.intervals)
                        if continuous is not None
                        else None,
                        intervals=continuous.intervals[query.offset : query.offset + query.limit]
                        if continuous is not None
                        else (),
                    )
                )
                if isinstance(result, bytes) and len(result) > MAX_LEDGER_WINDOW_INDEX_BYTES:
                    raise DomainError(
                        "USAGE_REPORT_TOO_LARGE", "Choose a shorter usage export window", 503
                    )
                # Recheck both domain scope and external-to-platform identity after presentation.
                self.history.access.require_in_transaction(
                    session, self.history.access_request(actor, selection, query.mode)
                )
                current = self.history.access.resolve_external_in_transaction(session, subject)
                if current is None or current.user_id != principal.user_id:
                    raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
                return result
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Usage database is unavailable", 503
            ) from None
