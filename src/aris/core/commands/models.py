from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Boolean,
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


class CommandRecord(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("actor_id", "key_sha256", name="uq_command_actor_key"),
        CheckConstraint(
            "state IN ('pending','awaiting_confirmation','queued','succeeded','expired',"
            "'cancelled','failed')",
            name="ck_command_state",
        ),
        CheckConstraint(
            "execution_deadline IS NULL OR (execution_not_after IS NOT NULL AND "
            "execution_deadline <= execution_not_after)",
            name="ck_command_execution_deadline",
        ),
        CheckConstraint(
            "state <> 'queued' OR execution_deadline IS NOT NULL", name="ck_command_queued_deadline"
        ),
        CheckConstraint("state <> 'failed' OR failure_code IS NOT NULL", name="ck_command_failure"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    key_sha256: Mapped[str] = mapped_column(String(64))
    command_type: Mapped[str] = mapped_column(String(128))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[
        Literal[
            "pending",
            "awaiting_confirmation",
            "queued",
            "succeeded",
            "expired",
            "cancelled",
            "failed",
        ]
    ] = mapped_column(String(32))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class CommandAdmission(Base):
    __tablename__ = "command_admissions"
    __table_args__ = (
        CheckConstraint(
            "max_queue_delay_seconds > 0 AND max_queue_delay_seconds <= 600",
            name="ck_admission_delay",
        ),
        CheckConstraint(
            "NOT confirmation_required OR confirmation_key_tag IS NOT NULL", name="ck_admission_key"
        ),
        CheckConstraint(
            "(verified_at IS NULL AND execution_deadline IS NULL AND verified_fingerprint IS "
            "NULL AND verification_kind IS NULL) OR "
            "(verified_at IS NOT NULL AND execution_deadline IS NOT NULL AND verified_at < "
            "execution_deadline AND "
            "verified_fingerprint IS NOT NULL AND ((confirmation_required AND "
            "verification_kind = 'proof') OR "
            "(NOT confirmation_required AND verification_kind = 'not_required')))",
            name="ck_admission_verification",
        ),
    )
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    namespace: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(128))
    definition_fingerprint: Mapped[str] = mapped_column(String(64))
    max_queue_delay_seconds: Mapped[int] = mapped_column(Integer)
    confirmation_required: Mapped[bool] = mapped_column(Boolean)
    confirmation_key_id: Mapped[str] = mapped_column(String(64))
    confirmation_key_tag: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_fingerprint: Mapped[str | None] = mapped_column(String(64))
    verification_kind: Mapped[Literal["proof", "not_required"] | None] = mapped_column(String(16))


class CommandIntakeReceipt(Base):
    __tablename__ = "command_intake_receipts"
    __table_args__ = (
        UniqueConstraint("actor_id", "key_sha256", name="uq_intake_actor_receipt"),
        CheckConstraint("kind IN ('submit','confirm','withdraw')", name="ck_intake_kind"),
        CheckConstraint("state IN ('receiving','accepted','rejected')", name="ck_intake_state"),
        CheckConstraint("attempts > 0", name="ck_intake_attempts"),
        CheckConstraint(
            "(state = 'receiving' AND error_code IS NULL AND error_status IS NULL) OR "
            "(state = 'accepted' AND command_id IS NOT NULL AND error_code IS NULL AND "
            "error_status IS NULL) OR "
            "(state = 'rejected' AND error_code IS NOT NULL AND error_status >= 400 AND "
            "error_status <= 599)",
            name="ck_intake_outcome",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # An ingress actor snapshot is not enrollment or a grant; rejected/unmapped
    # contexts can be audited without creating a users row.
    actor_id: Mapped[str] = mapped_column(String(36))
    key_sha256: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    subject_fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[Literal["submit", "confirm", "withdraw"]] = mapped_column(String(16))
    state: Mapped[Literal["receiving", "accepted", "rejected"]] = mapped_column(String(16))
    command_id: Mapped[str | None] = mapped_column(ForeignKey("commands.id"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_status: Mapped[int | None] = mapped_column(Integer)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    request_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommandJob(Base):
    __tablename__ = "command_jobs"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_command_job_command"),
        CheckConstraint(
            "state IN ('queued','running','succeeded','failed','expired','cancelled')",
            name="ck_command_job_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_command_job_attempts"),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'queued' AND lease_token IS NULL AND lease_expires_at IS NULL AND "
            "completed_at IS NULL) OR "
            "(state IN ('succeeded','failed','expired','cancelled') AND lease_token IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="ck_command_job_lease",
        ),
        Index("ix_command_jobs_ready", "state", "available_at", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"))
    state: Mapped[Literal["queued", "running", "succeeded", "failed", "expired", "cancelled"]] = (
        mapped_column(String(16))
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
