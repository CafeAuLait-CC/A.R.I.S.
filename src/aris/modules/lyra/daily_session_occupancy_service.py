"""Assess short gaps using the saved day's original ledger and identity revision."""

from datetime import timedelta
from time import monotonic

from sqlalchemy.orm import Session

from aris.core.commands.schemas import ActorContext
from aris.modules.lyra.daily_session_occupancy import (
    DailySessionOccupancy,
    clip_session_occupancy,
)
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.session_occupancy import SessionOccupancyRule
from aris.modules.lyra.session_occupancy_service import SessionOccupancyService
from aris.modules.lyra.usage_history import UsageHistoryQuery
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow


class DailySessionOccupancyService:
    def __init__(
        self, history: IndexedUsageHistoryService, occupancy: SessionOccupancyService
    ) -> None:
        self.history, self.occupancy = history, occupancy

    def read_in_transaction(
        self, db: Session, actor: ActorContext, snapshot: StoredUsageWindow
    ) -> DailySessionOccupancy:
        deadline = monotonic() + self.history.timeout_ms / 1000
        window = snapshot.window
        query = window.query
        padding = timedelta(seconds=SessionOccupancyRule().maximum_interruption_seconds)
        history = self.history.read_continuous_in_transaction(
            db,
            actor,
            UsageHistoryQuery(
                node_id=query.node_id,
                start_at=query.start_at - padding,
                end_at=min(query.end_at + padding, window.read_at),
                timezone=query.timezone,
                snapshot=window.ledger,
            ),
            mode="node",
            read_at=window.read_at,
        )
        occupancy = self.occupancy.read_in_transaction(
            db,
            history,
            deadline=deadline,
            identity_revision=window.identity_revision,
            freeze_identity=True,
        )
        return clip_session_occupancy(history, occupancy, query.start_at, query.end_at)
