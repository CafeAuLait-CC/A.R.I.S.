"""Saved-statistics presentation authenticates the caller and only invokes a reader."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract, UserLabelQuery
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.usage.service import CalendarWindow, UsageHistorySurface, UTCWindow
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.window_reader_schemas import (
    SavedWindowPage,
    SavedWindowView,
    WindowListQuery,
    WindowPageCursor,
)


class SavedUsageQuery(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    mode: Literal["mine", "node"] = "mine"
    owner_id: UUID | None = None
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]
    before: WindowPageCursor | None = None
    limit: int = Field(default=16, ge=1, le=32, strict=True)

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


def saved_usage(
    surface: UsageHistorySurface,
    authorization: str | None,
    request_id: str,
    query: SavedUsageQuery | SavedUsageRead,
) -> SavedWindowPage | SavedUsageDetail:
    subject = surface.authenticator.authenticate(authorization)
    if query.node_id not in surface.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this usage interface", 403)
    reader = surface.saved_windows
    if reader is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Saved statistics are not enabled", 503)
    try:
        with surface.history.sessions() as session:
            transaction_budget(
                session, 100, 1000, min(surface.history.timeout_ms, reader.timeout_ms)
            )
            principal = surface.history.access.resolve_external_in_transaction(session, subject)
            if principal is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
            result: SavedWindowPage | SavedUsageDetail
            if isinstance(query, SavedUsageRead):
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
                result = SavedUsageDetail(view=view, labels=labels)
                reader.require_in_transaction(
                    session, actor, view.query.node_id, view.query.owner_id, query.mode
                )
            else:
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
            current = surface.history.access.resolve_external_in_transaction(session, subject)
            if current is None or current.user_id != principal.user_id:
                raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
            return result
    except DBAPIError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Saved usage database is unavailable", 503
        ) from None
