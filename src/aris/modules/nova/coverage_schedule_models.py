from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class CoverageScheduleRow(Base):
    __tablename__ = "weekly_coverage_schedules"
    __table_args__ = (
        CheckConstraint(
            "revision>=1 AND next_week>=first_week AND updated_at>=created_at",
            name="ck_coverage_schedule_progress",
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_definition)>0 "
            "AND length(canonical_definition)<=16384",
            name="ck_coverage_schedule_definition",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_definition: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    first_week: Mapped[date] = mapped_column(Date)
    next_week: Mapped[date] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_result_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CoveragePeriodResultRow(Base):
    __tablename__ = "weekly_coverage_results"
    __table_args__ = (
        UniqueConstraint("schedule_id", "week", name="uq_coverage_result_week"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_result)>0 "
            "AND length(canonical_result)<=16384",
            name="ck_coverage_result_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("weekly_coverage_schedules.id"))
    week: Mapped[date] = mapped_column(Date)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("usage_window_snapshots.id"))
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_result: Mapped[str] = mapped_column(Text)


class DailyCoveragePeriodResultRow(Base):
    __tablename__ = "weekly_daily_coverage_results"
    __table_args__ = (
        UniqueConstraint("schedule_id", "week", name="uq_weekly_daily_result_period"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_result)>0 "
            "AND length(canonical_result)<=262144",
            name="ck_weekly_daily_result_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("weekly_coverage_schedules.id"))
    week: Mapped[date] = mapped_column(Date)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_result: Mapped[str] = mapped_column(Text)


class DailyCoverageEvidenceCheckRow(Base):
    __tablename__ = "weekly_daily_evidence_checks"
    __table_args__ = (
        CheckConstraint(
            "length(report_fingerprint)=64 AND length(snapshot_fingerprint)=64 "
            "AND length(rollup_fingerprint)=64",
            name="ck_weekly_evidence_check_fingerprints",
        ),
    )

    report_id: Mapped[str] = mapped_column(
        ForeignKey("weekly_daily_coverage_results.id"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    report_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64))
    rollup_fingerprint: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VerifiedDailyCoverageRevisionRow(Base):
    __tablename__ = "weekly_daily_verified_revisions"
    __table_args__ = (
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_revision) BETWEEN 1 AND 262144",
            name="ck_weekly_daily_verified_revision",
        ),
    )

    original_report_id: Mapped[str] = mapped_column(
        ForeignKey("weekly_daily_coverage_results.id"), primary_key=True
    )
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_revision: Mapped[str] = mapped_column(Text)
