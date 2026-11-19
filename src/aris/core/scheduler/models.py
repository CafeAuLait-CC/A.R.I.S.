from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class ScheduledJobRow(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        Index("ix_scheduled_jobs_due", "kind", "state", "available_at"),
        CheckConstraint(
            "state IN ('pending','running','retry','completed','blocked','dead')",
            name="ck_scheduled_job_state",
        ),
        CheckConstraint(
            "attempts>=0 AND attempts<=10 AND updated_at>=created_at AND available_at>=created_at",
            name="ck_scheduled_job_bounds",
        ),
        CheckConstraint(
            "((state='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL))",
            name="ck_scheduled_job_lease",
        ),
        CheckConstraint(
            "((state='completed' AND canonical_result IS NOT NULL) "
            "OR (state<>'completed' AND canonical_result IS NULL))",
            name="ck_scheduled_job_result",
        ),
        CheckConstraint(
            "((state IN ('retry','blocked','dead') AND error_code IS NOT NULL) "
            "OR (state NOT IN ('retry','blocked','dead') AND error_code IS NULL))",
            name="ck_scheduled_job_error",
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_request)>0 "
            "AND length(canonical_request)<=16384",
            name="ck_scheduled_job_input",
        ),
        CheckConstraint(
            "canonical_result IS NULL OR length(canonical_result)<=16384",
            name="ck_scheduled_job_output",
        ),
        CheckConstraint(
            "(attempts=0 AND last_run_id IS NULL) OR (attempts>0 AND last_run_id IS NOT NULL)",
            name="ck_scheduled_job_run",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_key: Mapped[str] = mapped_column(String(512), unique=True)
    kind: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[str] = mapped_column(String(36))
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(128))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    canonical_request: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canonical_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScheduledJobRunRow(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_scheduled_run_attempt"),
        CheckConstraint(
            "attempt>=1 AND attempt<=10 AND lease_expires_at>started_at",
            name="ck_scheduled_run_bounds",
        ),
        CheckConstraint(
            "state IN ('running','completed','retry','blocked','dead','expired')",
            name="ck_scheduled_run_state",
        ),
        CheckConstraint(
            "((state='running' AND finished_at IS NULL) "
            "OR (state<>'running' AND finished_at IS NOT NULL AND finished_at>=started_at))",
            name="ck_scheduled_run_finished",
        ),
        CheckConstraint(
            "((state='completed' AND canonical_result IS NOT NULL) "
            "OR (state<>'completed' AND canonical_result IS NULL))",
            name="ck_scheduled_run_result",
        ),
        CheckConstraint(
            "canonical_result IS NULL OR length(canonical_result)<=16384",
            name="ck_scheduled_run_output",
        ),
        CheckConstraint(
            "((state IN ('retry','blocked','dead','expired') AND error_code IS NOT NULL) "
            "OR (state NOT IN ('retry','blocked','dead','expired') AND error_code IS NULL))",
            name="ck_scheduled_run_error",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("scheduled_jobs.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str] = mapped_column(String(36), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
    canonical_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
