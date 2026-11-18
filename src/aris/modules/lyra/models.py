from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class UsageDerivation(Base):
    __tablename__ = "usage_derivations"
    __table_args__ = (
        CheckConstraint(
            "source_count >= 1 AND source_count <= 1024", name="ck_usage_derivation_sources"
        ),
        CheckConstraint("identity_revision > 0", name="ck_usage_derivation_identity"),
        CheckConstraint(
            "length(request_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_usage_derivation_fingerprints",
        ),
        CheckConstraint(
            "length(canonical_request) > 0 AND length(canonical_request) <= 262144",
            name="ck_usage_derivation_request",
        ),
        CheckConstraint(
            "length(canonical_result) > 0 AND length(canonical_result) <= 4194304",
            name="ck_usage_derivation_result",
        ),
        CheckConstraint("created_at >= as_of", name="ck_usage_derivation_time"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    agent_id: Mapped[str] = mapped_column(String(128))
    node_id: Mapped[str] = mapped_column(String(128))
    source_count: Mapped[int] = mapped_column(Integer)
    identity_revision: Mapped[int] = mapped_column(BigInteger)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    result_fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_request: Mapped[str] = mapped_column(Text)
    canonical_result: Mapped[str] = mapped_column(Text)
