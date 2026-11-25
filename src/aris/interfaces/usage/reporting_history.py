"""Current-node authorized presentation of saved abnormal reporting intervals."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract
from aris.core.commands.budgets import database_failure
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.usage.activity_service import _current_subject
from aris.modules.lyra.reporting_gap_history_service import ReportingGapHistoryService

if TYPE_CHECKING:
    from aris.interfaces.usage.service import UsageHistorySurface


class ReportingHistoryInput(AccessContract):
    view: Literal["reporting_history"]
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    limit: int = Field(default=16, ge=1, le=32, strict=True)
    before_seq: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def bounded_cursor(self) -> "ReportingHistoryInput":
        if self.before_seq is not None and int(self.before_seq) > 9223372036854775807:
            raise ValueError("Reporting history cursor exceeds the supported sequence")
        return self


class ReportingHistoryItem(AccessContract):
    report_start_at: AwareDatetime
    report_end_at: AwareDatetime
    received_start_at: AwareDatetime
    received_end_at: AwareDatetime
    large_report_interval: bool
    large_received_interval: bool
    cause: Literal["unknown"] = "unknown"


class ReportingHistoryPage(AccessContract):
    node_id: str
    items: tuple[ReportingHistoryItem, ...] = Field(max_length=32)
    next_before_seq: str | None
    read_at: AwareDatetime


def reporting_history(
    surface: "UsageHistorySurface",
    authorization: str | None,
    request_id: str,
    query: ReportingHistoryInput,
) -> ReportingHistoryPage:
    try:
        return _read_history(surface, authorization, request_id, query)
    except DBAPIError as error:
        code = database_failure(error)
        raise DomainError(code, "Reporting history database is unavailable", 503) from None


def _read_history(
    surface: "UsageHistorySurface",
    authorization: str | None,
    request_id: str,
    query: ReportingHistoryInput,
) -> ReportingHistoryPage:
    subject = surface.authenticator.authenticate(authorization)
    query = ReportingHistoryInput.model_validate(query.model_dump(mode="json"))
    access = surface.activity
    if access is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Archive history is unavailable", 503)
    if query.node_id not in surface.node_ids or query.node_id not in access.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this history interface", 403)
    user = _current_subject(surface, subject, query.node_id)
    source = next(item for item in access.config.sources if item.node_id == query.node_id)
    reader = ReportingGapHistoryService(access.reader)
    reader.access = access.reader.access
    saved = reader.latest(
        ActorContext(actor_id=user, request_id=request_id),
        source,
        limit=query.limit + 1,
        before_seq=int(query.before_seq) if query.before_seq is not None else None,
    )
    selected = saved[: query.limit]
    if _current_subject(surface, subject, query.node_id) != user:
        raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
    return ReportingHistoryPage(
        node_id=query.node_id,
        items=tuple(
            ReportingHistoryItem(
                report_start_at=value.gap.previous_report.observed_at,
                report_end_at=value.gap.next_report.observed_at,
                received_start_at=value.gap.previous_report.received_at,
                received_end_at=value.gap.next_report.received_at,
                large_report_interval=value.gap.large_observed_interval,
                large_received_interval=value.gap.large_received_interval,
            )
            for value in selected
        ),
        next_before_seq=selected[-1].gap.next_report.seq if len(saved) > query.limit else None,
        read_at=datetime.now(UTC),
    )
