import hashlib

from sqlalchemy import and_, func, or_, select, text, true
from sqlalchemy.orm import Session

from aris.core.time import utc
from aris.modules.nova.availability_schemas import (
    CONFLICT_PREVIEW_LIMIT,
    AvailabilityWindow,
    ConflictSummary,
)
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import MineQuery, ReservationView
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

    def conflict_summary(
        self,
        gpu_uuid: str,
        window: ReservationWindow,
        *,
        exclude_reservation_id: str | None = None,
    ) -> ConflictSummary:
        # Count and bounded preview share one statement's MVCC snapshot.
        rows = self.session.execute(
            select(Reservation.start_at, Reservation.end_at, func.count().over())
            .where(
                Reservation.gpu_uuid == gpu_uuid,
                Reservation.status.in_(BLOCKING_STATUSES),
                Reservation.start_at < window.end_at,
                Reservation.end_at > window.start_at,
                Reservation.id != exclude_reservation_id
                if exclude_reservation_id is not None
                else true(),
            )
            .order_by(Reservation.start_at, Reservation.end_at, Reservation.id)
            .limit(CONFLICT_PREVIEW_LIMIT)
        ).all()
        total = int(rows[0][2]) if rows else 0
        windows = tuple(
            AvailabilityWindow(start_at=utc(row[0]), end_at=utc(row[1])) for row in rows
        )
        return ConflictSummary(total=total, windows=windows, truncated=total > len(windows))

    def lock_target(self, gpu_uuid: str) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            lock_id = int.from_bytes(
                hashlib.sha256(b"aris.nova.target\x00" + gpu_uuid.encode()).digest()[:8],
                signed=True,
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id}
            )
        elif dialect != "sqlite":
            raise RuntimeError("unsupported reservation database")

    def add(self, reservation: Reservation) -> None:
        self.session.add(reservation)

    def append_event(self, event: ReservationEvent) -> None:
        self.session.add(event)

    def reference(self, reservation_id: str) -> tuple[str, str] | None:
        row = self.session.execute(
            select(Reservation.owner_id, Reservation.gpu_uuid).where(
                Reservation.id == reservation_id
            )
        ).one_or_none()
        return (row[0], row[1]) if row is not None else None

    def lock_reservation(self, reservation_id: str) -> Reservation | None:
        return self.session.scalar(
            select(Reservation)
            .where(Reservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def history_version(self, reservation_id: str) -> int | None:
        version = self.session.scalar(
            select(func.max(ReservationEvent.version)).where(
                ReservationEvent.reservation_id == reservation_id
            )
        )
        return int(version) if version is not None else None

    def mine(self, owner_id: str, query: MineQuery) -> tuple[ReservationView, ...]:
        version = (
            select(func.max(ReservationEvent.version))
            .where(ReservationEvent.reservation_id == Reservation.id)
            .correlate(Reservation)
            .scalar_subquery()
        )
        statement = select(
            Reservation.id,
            Reservation.gpu_uuid,
            Reservation.start_at,
            Reservation.end_at,
            Reservation.status,
            Reservation.purpose,
            Reservation.policy_version,
            Reservation.created_at,
            Reservation.updated_at,
            version.label("version"),
        ).where(Reservation.owner_id == owner_id)
        if query.cursor is not None:
            statement = statement.where(
                or_(
                    Reservation.created_at < query.cursor.created_at,
                    and_(
                        Reservation.created_at == query.cursor.created_at,
                        Reservation.id < str(query.cursor.reservation_id),
                    ),
                )
            )
        rows = self.session.execute(
            statement.order_by(Reservation.created_at.desc(), Reservation.id.desc()).limit(
                query.limit + 1
            )
        ).mappings()
        return tuple(
            ReservationView.model_validate(
                {
                    **row,
                    **{
                        name: utc(row[name])
                        for name in ("start_at", "end_at", "created_at", "updated_at")
                    },
                }
            )
            for row in rows
        )
