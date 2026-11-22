from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class DailyUsageRollupRow(Base):
    __tablename__ = "usage_daily_rollups"
    __table_args__ = (
        CheckConstraint(
            "length(fingerprint)=64 AND length(snapshot_fingerprint)=64 "
            "AND length(canonical_rollup)>0 AND length(canonical_rollup)<=262144",
            name="ck_daily_usage_rollup_size",
        ),
    )

    # Provenance remains meaningful when the original interval snapshot expires.
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(128))
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_rollup: Mapped[str] = mapped_column(Text)
