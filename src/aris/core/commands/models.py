from datetime import datetime
from typing import Any, Literal

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class CommandRecord(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("actor_id", "key_sha256", name="uq_command_actor_key"),
        CheckConstraint("state IN ('pending','succeeded')", name="ck_command_state"),
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
    state: Mapped[Literal["pending", "succeeded"]] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
