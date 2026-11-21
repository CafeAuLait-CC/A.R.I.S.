"""Saved-statistics presentation authenticates the caller and only invokes a reader."""

from time import monotonic
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope, UserLabelQuery
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.usage.saved_report import saved_csv, saved_report
from aris.interfaces.usage.service import CalendarWindow, UsageHistorySurface, UTCWindow
from aris.modules.lyra.ledger_window_read import MAX_LEDGER_WINDOW_INDEX_BYTES
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.window_reader_schemas import (
    SavedWindowPage,
    SavedWindowView,
    WindowListQuery,
    WindowPageCursor,
)
from aris.modules.nova.coverage_schedule_reader import CoveragePlanPage
from aris.modules.vega.target_schemas import GpuLabelQuery


class SavedUsageQuery(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"
    owner_id: UUID | None = None
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]
    before: WindowPageCursor | None = None
    limit: int = Field(default=16, ge=1, le=32, strict=True)
    include_plans: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def own_scope(self) -> "SavedUsageQuery":
        if self.mode == "mine" and self.owner_id is not None:
            raise ValueError("personal saved statistics always use the current user")
        return self


class SavedUsageRead(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"
    id: UUID


class SavedUsageDetail(AccessContract):
    view: SavedWindowView
    labels: dict[str, str]
    gpu_indices: dict[str, int] = Field(default_factory=dict)


class SavedUsagePlanPage(AccessContract):
    snapshots: SavedWindowPage
    plans: CoveragePlanPage


def saved_usage(
    surface: UsageHistorySurface,
    authorization: str | None,
    request_id: str,
    query: SavedUsageQuery | SavedUsageRead,
    *,
    output: Literal["json", "html", "csv"] = "json",
) -> SavedWindowPage | SavedUsagePlanPage | SavedUsageDetail | bytes:
    subject = surface.authenticator.authenticate(authorization)
    if query.node_id not in surface.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    reader = surface.saved_windows
    if reader is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Saved statistics are not enabled", 503)
    deadline = monotonic() + min(surface.history.timeout_ms, reader.timeout_ms) / 1000
    try:
        with surface.history.sessions() as session:
            transaction_budget(
                session, 100, 1000, min(surface.history.timeout_ms, reader.timeout_ms)
            )
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            result: SavedWindowPage | SavedUsagePlanPage | SavedUsageDetail | bytes
            if isinstance(query, SavedUsageRead):
                if output != "json":
                    surface.history.access.require_in_transaction(
                        session,
                        AccessRequest(
                            user_id=principal.user_id,
                            scope=AccessScope(type="node", id=query.node_id),
                            permission="usage.read_own"
                            if query.mode == "mine"
                            else "usage.read_all",
                        ),
                    )
                view = reader.read_in_transaction(
                    session, actor, query.id, node_id=query.node_id, mode=query.mode
                )
                owners = sorted(
                    {cell.owner_id for cell in view.cells if cell.owner_id is not None}, key=str
                )
                labels = {}
                for offset in range(0, len(owners), 128):
                    labels.update(
                        {
                            str(value.user_id): value.display_name
                            for value in surface.history.access.labels_in_transaction(
                                session,
                                UserLabelQuery(user_ids=tuple(owners[offset : offset + 128])),
                            )
                        }
                    )
                gpu_indices = {}
                if surface.gpu_labels is not None and output != "csv":
                    uuids = tuple(sorted({cell.gpu_uuid for cell in view.cells}))
                    for offset in range(0, len(uuids), 100):
                        gpu_indices.update(
                            {
                                label.gpu_uuid: label.index
                                for label in surface.gpu_labels.labels_in_transaction(
                                    session, GpuLabelQuery(gpu_uuids=uuids[offset : offset + 100])
                                )
                                if label.node_id == view.query.node_id
                            }
                        )
                result = (
                    SavedUsageDetail(view=view, labels=labels, gpu_indices=gpu_indices)
                    if output == "json"
                    else saved_report(view, labels, gpu_indices)
                    if output == "html"
                    else saved_csv(view, labels)
                )
                if isinstance(result, bytes) and len(result) > MAX_LEDGER_WINDOW_INDEX_BYTES:
                    raise DomainError("USAGE_REPORT_TOO_LARGE", "Saved export is too large", 503)
                reader.require_in_transaction(
                    session, actor, view.query.node_id, view.query.owner_id, query.mode
                )
            else:
                if output != "json":
                    raise DomainError("VALIDATION_ERROR", "Export requires a saved snapshot", 422)
                if isinstance(query.window, CalendarWindow):
                    bounds = calendar_boundaries(
                        query.window.start_date, query.window.end_date, query.window.timezone
                    )
                    start, end = bounds[0], min(bounds[-1], surface.history._now())
                else:
                    start, end = query.window.start_at, query.window.end_at
                try:
                    selection = WindowListQuery(
                        node_id=query.node_id,
                        owner_id=principal.user_id if query.mode == "mine" else query.owner_id,
                        start_at=start,
                        end_at=end,
                        timezone=query.window.timezone,
                        before=query.before,
                        limit=query.limit,
                    )
                except ValueError:
                    raise DomainError(
                        "VALIDATION_ERROR", "Saved statistics date range is invalid", 422
                    ) from None
                result = reader.list_in_transaction(session, actor, selection, mode=query.mode)
                if query.include_plans:
                    if surface.coverage_plans is None:
                        raise DomainError(
                            "USAGE_NOT_CONFIGURED", "Coverage plan reading is not enabled", 503
                        )
                    result = SavedUsagePlanPage(
                        snapshots=result,
                        plans=surface.coverage_plans.read_in_transaction(
                            session, actor, node_id=query.node_id, mode=query.mode
                        ),
                    )
            current = surface.history.access.resolve_external_in_transaction(session, subject)
            if current is None or current.user_id != principal.user_id:
                raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Saved export budget exceeded", 503)
            return result
    except DBAPIError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Saved usage database is unavailable", 503
        ) from None
