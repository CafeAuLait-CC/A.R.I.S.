from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base
from aris.modules.nova.schemas import ReservationStatus


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("start_at < end_at", name="ck_reservation_window"),
        CheckConstraint(
            "status IN ('pending','confirmed','active','completed',"
            "'cancelled','rejected','reclaiming')",
            name="ck_reservation_status",
        ),
        # SQLAlchemy 2.0's dialect constructor lacks a typed signature.
        ExcludeConstraint(  # type: ignore[no-untyped-call]
            ("gpu_uuid", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            name="ex_reservation_gpu_interval",
            using="gist",
            where=text("status IN ('confirmed','active','reclaiming')"),
            ops={"gpu_uuid": "public.gist_text_ops"},
        ).ddl_if(dialect="postgresql"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # The future service validates this stable opaque reference through Vega's
    # public contract, without importing/querying its private tables here.
    gpu_uuid: Mapped[str] = mapped_column(String(128), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReservationStatus] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(256))
    policy_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReservationEvent(Base):
    __tablename__ = "reservation_events"
    __table_args__ = (
        UniqueConstraint("reservation_id", "version", name="uq_reservation_event_version"),
        CheckConstraint("version > 0", name="ck_reservation_event_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
