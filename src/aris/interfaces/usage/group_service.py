"""Current caller, explicit allowed nodes and fixed-prefix exports of recorded group usage."""

from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract, Identifier, UserLabelQuery
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.usage.group_report import group_html_report, group_summary_csv
from aris.interfaces.usage.service import CalendarWindow, UsageHistorySurface, UTCWindow
from aris.modules.lyra.group_usage import GroupUsageOwner, GroupUsageQuery
from aris.modules.lyra.group_usage_service import GroupUsageService
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.usage_history import UsageHistoryDay, calendar_boundaries


class GroupUsageInput(AccessContract):
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    mode: Literal["mine", "node"] = "mine"
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]
    snapshots: tuple[UsageLedgerHead, ...] | None = Field(default=None, max_length=8)

    @field_validator("node_ids")
    @classmethod
    def unique_nodes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("selected nodes must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def fixed_nodes(self) -> "GroupUsageInput":
        if self.snapshots is not None and (
            self.window.kind != "utc"
            or tuple(sorted(head.node_id for head in self.snapshots)) != self.node_ids
        ):
            raise ValueError("fixed group exports require every selected node and a UTC window")
        return self


class GroupNodeSummary(AccessContract):
    node_id: str
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    snapshot: UsageLedgerHead
    source_coverage: Literal["unknown"] = "unknown"


class GroupUsageSummary(AccessContract):
    assessment_scope: Literal["selected_nodes_recorded_usage"] = "selected_nodes_recorded_usage"
    weighted: Literal[False] = False
    query: GroupUsageQuery
    owner_id: UUID | None
    recorded_microseconds: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    nodes: tuple[GroupNodeSummary, ...]
    owners: tuple[GroupUsageOwner, ...]
    days: tuple[UsageHistoryDay, ...]
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    snapshot_scope: Literal["independent_fixed_node_prefixes"] = "independent_fixed_node_prefixes"
    fingerprint: str


class GroupUsagePage(AccessContract):
    report: GroupUsageSummary
    labels: dict[str, str]


def group_usage(
    surface: UsageHistorySurface,
    authorization: str | None,
    request_id: str,
    request: GroupUsageInput,
    *,
    output: Literal["page", "html", "csv"] = "page",
) -> GroupUsagePage | bytes:
    subject = surface.authenticator.authenticate(authorization)
    request = GroupUsageInput.model_validate(request.model_dump(mode="json"))
    if any(node not in surface.node_ids for node in request.node_ids):
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    if output not in ("page", "html", "csv") or (output != "page" and request.snapshots is None):
        raise DomainError("VALIDATION_ERROR", "Export requires the displayed node snapshots", 422)
    if isinstance(request.window, CalendarWindow):
        bounds = calendar_boundaries(
            request.window.start_date, request.window.end_date, request.window.timezone
        )
        start, end = bounds[0], min(bounds[-1], surface.history._now())
    else:
        start, end = request.window.start_at, request.window.end_at
    try:
        query = GroupUsageQuery(
            node_ids=request.node_ids,
            start_at=start,
            end_at=end,
            timezone=request.window.timezone,
            snapshots=request.snapshots,
        )
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "Invalid selected-node date range", 422) from None
    service = GroupUsageService(surface.history)
    deadline = monotonic() + service.timeout_ms / 1000
    try:
        with surface.history.sessions() as session:
            transaction_budget(session, 100, 1000, service.timeout_ms)
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            report = service.read_in_transaction(session, actor, query, mode=request.mode)
            owners = {value.owner_id for value in report.owners}
            if report.owner_id is not None:
                owners.add(report.owner_id)
            ids = tuple(sorted(owners, key=str))
            labels = {}
            for offset in range(0, len(ids), 128):
                labels.update(
                    {
                        str(value.user_id): value.display_name
                        for value in surface.history.access.labels_in_transaction(
                            session, UserLabelQuery(user_ids=ids[offset : offset + 128])
                        )
                    }
                )
            result: GroupUsagePage | bytes
            if output == "csv":
                result = group_summary_csv(report, labels)
            elif output == "html":
                result = group_html_report(report, labels)
            else:
                summary = GroupUsageSummary(
                    query=report.query,
                    owner_id=report.owner_id,
                    recorded_microseconds=report.recorded_microseconds,
                    segment_count=report.segment_count,
                    nodes=tuple(
                        GroupNodeSummary(
                            node_id=node.node_id,
                            recorded_microseconds=node.recorded_microseconds,
                            gpu_count=node.gpu_count,
                            segment_count=node.segment_count,
                            snapshot=node.snapshot,
                        )
                        for node in report.histories
                    ),
                    owners=report.owners,
                    days=report.days,
                    fingerprint=report.fingerprint,
                )
                result = GroupUsagePage(report=summary, labels=labels)
            service.require_in_transaction(session, actor, query, request.mode)
            current = surface.history.access.resolve_external_in_transaction(session, subject)
            if current is None or current.user_id != principal.user_id:
                raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
            if monotonic() >= deadline:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Selected-node rendering budget exceeded", 503
                )
            return result
    except DBAPIError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Selected-node usage is unavailable", 503
        ) from None
