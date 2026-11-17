from datetime import datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class EventPublication(Base):
    __tablename__ = "event_publications"
    __table_args__ = (CheckConstraint("subscriber_count > 0", name="ck_publication_subscribers"),)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), primary_key=True)
    event_fingerprint: Mapped[str] = mapped_column(String(64))
    registry_fingerprint: Mapped[str] = mapped_column(String(64))
    subscriber_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer_id", name="uq_delivery_event_consumer"),
        CheckConstraint(
            "state IN ('queued','running','processed','dead')", name="ck_delivery_state"
        ),
        CheckConstraint("attempts >= 0 AND replay_count >= 0", name="ck_delivery_attempts"),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL) OR (state = 'queued' AND lease_token IS NULL AND "
            "lease_expires_at IS NULL AND completed_at IS NULL) OR (state IN "
            "('processed','dead') AND lease_token IS NULL AND lease_expires_at IS NULL AND "
            "completed_at IS NOT NULL)",
            name="ck_delivery_lease",
        ),
        Index("ix_event_deliveries_ready", "state", "available_at", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("event_publications.event_id"))
    consumer_id: Mapped[str] = mapped_column(String(128))
    definition_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[Literal["queued", "running", "processed", "dead"]] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    replay_count: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsumerInbox(Base):
    __tablename__ = "event_consumer_inbox"
    consumer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("event_deliveries.id"))
    event_fingerprint: Mapped[str] = mapped_column(String(64))
    definition_fingerprint: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
