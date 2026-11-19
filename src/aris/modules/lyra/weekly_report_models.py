from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class WeeklyReportRow(Base):
    __tablename__ = "usage_weekly_reports"
    __table_args__ = (
        Index("ix_usage_weekly_node_period", "node_id", "start_at", "end_at"),
        CheckConstraint("end_at>start_at AND created_at>=end_at", name="ck_usage_weekly_period"),
        CheckConstraint(
            "recorded_microseconds>=0 AND ledger_revision>=0", name="ck_usage_weekly_totals"
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_report)>0 "
            "AND length(canonical_report)<=4194304",
            name="ck_usage_weekly_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    node_id: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    week: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(128))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_microseconds: Mapped[int] = mapped_column(BigInteger)
    ledger_revision: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_report: Mapped[str] = mapped_column(Text)
