from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.usage_link_schemas import MAX_RESERVATION_LINK_HISTORY


@dataclass(frozen=True)
class UsageLinkHistory:
    owner_id: str
    gpu_uuid: str
    start_at: datetime
    end_at: datetime
    status: str
    updated_at: datetime
    id: str | None
    version: int | None
    event_type: str | None
    actor: str | None
    occurred_at: datetime | None
    payload: dict[str, Any] | None


class ReservationUsageLinkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def history(self, reservation_id: str) -> tuple[UsageLinkHistory, ...]:
        rows = self.session.execute(
            select(
                Reservation.owner_id,
                Reservation.gpu_uuid,
                Reservation.start_at,
                Reservation.end_at,
                Reservation.status,
                Reservation.updated_at,
                ReservationEvent.id,
                ReservationEvent.version,
                ReservationEvent.event_type,
                ReservationEvent.actor,
                ReservationEvent.occurred_at,
                ReservationEvent.payload,
            )
            .outerjoin(ReservationEvent, ReservationEvent.reservation_id == Reservation.id)
            .where(Reservation.id == reservation_id)
            .order_by(ReservationEvent.version)
            .limit(MAX_RESERVATION_LINK_HISTORY + 1)
        ).all()
        if len(rows) > MAX_RESERVATION_LINK_HISTORY:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Reservation history exceeds its budget", 503
            )
        return tuple(UsageLinkHistory(*row) for row in rows)
