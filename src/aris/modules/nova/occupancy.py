from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from aris.core.time import utc
from aris.modules.nova.occupancy_schemas import (
    CurrentReservationQuery,
    CurrentReservationSnapshot,
    GpuReservationOccupancy,
)
from aris.modules.nova.repo import ReservationRepository


class ReservationOccupancyService:
    """Internal read contract; adapters must authorize disclosure separately."""

    def __init__(
        self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    def current(self, query: CurrentReservationQuery) -> CurrentReservationSnapshot:
        with self.sessions() as session:
            return self.current_in_transaction(session, query)

    def current_in_transaction(
        self, session: Session, query: CurrentReservationQuery
    ) -> CurrentReservationSnapshot:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reservation occupancy clock requires a timezone")
        now = utc(now)
        with session.no_autoflush:
            rows = ReservationRepository(session).current_occupancy(query.gpu_uuids, now)
        return CurrentReservationSnapshot(
            as_of=now,
            gpus=tuple(
                GpuReservationOccupancy(
                    gpu_uuid=target,
                    state="unoccupied"
                    if target not in rows
                    else "occupied"
                    if rows[target][0] == 1
                    else "unknown",
                    ends_at=utc(rows[target][1])
                    if target in rows and rows[target][0] == 1
                    else None,
                )
                for target in query.gpu_uuids
            ),
        )
