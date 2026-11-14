from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class DomainEvent(Base):
    __tablename__ = "domain_events"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128))
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(128))
    aggregate_version: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String(128))
    correlation_id: Mapped[str] = mapped_column(String(128))
    causation_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class OutboxEntry(Base):
    __tablename__ = "event_outbox"
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
