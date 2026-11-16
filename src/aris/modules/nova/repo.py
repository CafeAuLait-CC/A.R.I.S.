from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.core.time import utc
from aris.modules.nova.availability_schemas import (
    CONFLICT_PREVIEW_LIMIT,
    AvailabilityWindow,
    ConflictSummary,
)
from aris.modules.nova.models import Reservation
from aris.modules.nova.schemas import BLOCKING_STATUSES, ReservationWindow


class ReservationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def overlapping(self, gpu_uuid: str, window: ReservationWindow) -> list[Reservation]:
        """Candidate conflicts; the PG constraint remains the write authority."""
        return list(
            self.session.scalars(
                select(Reservation)
                .where(
                    Reservation.gpu_uuid == gpu_uuid,
                    Reservation.status.in_(BLOCKING_STATUSES),
                    Reservation.start_at < window.end_at,
                    Reservation.end_at > window.start_at,
                )
                .order_by(Reservation.start_at, Reservation.id)
            )
        )

    def conflict_summary(self, gpu_uuid: str, window: ReservationWindow) -> ConflictSummary:
        # Count and bounded preview share one statement's MVCC snapshot.
        rows = self.session.execute(
            select(Reservation.start_at, Reservation.end_at, func.count().over())
            .where(
                Reservation.gpu_uuid == gpu_uuid,
                Reservation.status.in_(BLOCKING_STATUSES),
                Reservation.start_at < window.end_at,
                Reservation.end_at > window.start_at,
            )
            .order_by(Reservation.start_at, Reservation.end_at, Reservation.id)
            .limit(CONFLICT_PREVIEW_LIMIT)
        ).all()
        total = int(rows[0][2]) if rows else 0
        windows = tuple(
            AvailabilityWindow(start_at=utc(row[0]), end_at=utc(row[1])) for row in rows
        )
        return ConflictSummary(total=total, windows=windows, truncated=total > len(windows))
