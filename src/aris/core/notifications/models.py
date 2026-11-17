from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
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


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_sha256", name="uq_notification_dedupe"),
        CheckConstraint(
            "state IN ('queued','running','delivered','failed','uncertain')",
            name="ck_notification_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_notification_attempts"),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND last_attempt_id IS NOT NULL AND last_attempt_id = lease_token AND "
            "completed_at IS NULL) OR (state = 'queued' "
            "AND lease_token IS NULL AND lease_expires_at IS NULL AND completed_at IS NULL) OR "
            "(state IN ('delivered','failed','uncertain') AND lease_token IS NULL AND "
            "lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="ck_notification_lease",
        ),
        CheckConstraint(
            "state NOT IN ('delivered','uncertain') OR "
            "(last_attempt_id IS NOT NULL AND attempts > 0)",
            name="ck_notification_evidence",
        ),
        Index("ix_notifications_ready", "state", "available_at", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"))
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(64))
    namespace: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(128))
    template_id: Mapped[str] = mapped_column(String(128))
    template_fingerprint: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    dedupe_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[Literal["queued", "running", "delivered", "failed", "uncertain"]] = mapped_column(
        String(16)
    )
    request_id: Mapped[str] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer)
    last_attempt_id: Mapped[str | None] = mapped_column(String(36))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"
    __table_args__ = (
        UniqueConstraint("notification_id", "attempt", name="uq_notification_attempt"),
        CheckConstraint("attempt > 0", name="ck_notification_attempt_number"),
        CheckConstraint(
            "state IN ('claimed','sending','delivered','rejected','uncertain',"
            "'abandoned','late_delivered')",
            name="ck_notification_attempt_state",
        ),
        CheckConstraint(
            "(state IN ('claimed','sending') AND finished_at IS NULL) OR "
            "(state IN ('delivered','rejected','uncertain','abandoned','late_delivered') AND "
            "finished_at IS NOT NULL)",
            name="ck_notification_attempt_finished",
        ),
        CheckConstraint(
            "(state IN ('delivered','late_delivered') AND channel_ref IS NOT NULL AND "
            "message_ref IS NOT NULL) OR (state NOT IN ('delivered','late_delivered') AND "
            "channel_ref IS NULL AND message_ref IS NULL)",
            name="ck_notification_attempt_receipt",
        ),
        CheckConstraint(
            "(state IN ('claimed','abandoned') AND prepared_at IS NULL) OR "
            "(state IN ('sending','uncertain','delivered','late_delivered') AND "
            "prepared_at IS NOT NULL) OR state = 'rejected'",
            name="ck_notification_attempt_phase",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notification_id: Mapped[str] = mapped_column(ForeignKey("notification_outbox.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    state: Mapped[
        Literal[
            "claimed",
            "sending",
            "delivered",
            "rejected",
            "uncertain",
            "abandoned",
            "late_delivered",
        ]
    ] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    channel_ref: Mapped[str | None] = mapped_column(String(128))
    message_ref: Mapped[str | None] = mapped_column(String(128))
