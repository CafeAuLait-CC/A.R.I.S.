from sqlalchemy import select
from sqlalchemy.orm import Session

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
