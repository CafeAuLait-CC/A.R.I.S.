from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class WeeklyPlanRow(Base):
    __tablename__ = "weekly_report_plans"
    __table_args__ = (
        Index("ix_weekly_plan_scope", "node_id", "owner_id", "report_type"),
        CheckConstraint(
            "next_week>=first_week AND updated_at>=created_at AND revision>=1",
            name="ck_weekly_plan_progress",
        ),
        CheckConstraint(
            "consecutive_failures>=0 AND consecutive_failures<=10", name="ck_weekly_plan_failures"
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_weekly_plan_lease",
        ),
        CheckConstraint(
            "(last_error_code IS NULL AND retry_at IS NULL AND consecutive_failures=0) "
            "OR (last_error_code IS NOT NULL AND retry_at IS NOT NULL AND consecutive_failures>0)",
            name="ck_weekly_plan_error",
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_plan)>0 AND length(canonical_plan)<=16384",
            name="ck_weekly_plan_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_key: Mapped[str] = mapped_column(String(512), unique=True)
    node_id: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[str] = mapped_column(String(36))
    report_type: Mapped[str] = mapped_column(String(64))
    first_week: Mapped[date] = mapped_column(Date)
    canonical_plan: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(BigInteger)
    next_week: Mapped[date] = mapped_column(Date)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_prepared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
