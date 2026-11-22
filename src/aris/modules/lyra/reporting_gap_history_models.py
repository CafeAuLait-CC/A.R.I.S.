from sqlalchemy import BigInteger, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class ReportingGapHistoryRow(Base):
    __tablename__ = "reporting_gap_history"
    __table_args__ = (
        CheckConstraint(
            "next_seq > 1 AND length(canonical_record) BETWEEN 1 AND 4096",
            name="ck_reporting_gap_history_value",
        ),
    )

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    next_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_record: Mapped[str] = mapped_column(Text)
