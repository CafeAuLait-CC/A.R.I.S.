from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class AuditEntry(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changes: Mapped[dict[str, str | int | bool]] = mapped_column(JSON)
