"""Mutable login state; authentication secrets are never stored in plaintext."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class LoginCredential(Base):
    __tablename__ = "user_login_credentials"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="ck_login_credential_digest"),
        CheckConstraint("generation >= 1", name="ck_login_credential_generation"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    generation: Mapped[int]
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    __tablename__ = "user_login_sessions"
    __table_args__ = (
        CheckConstraint("length(token_digest) = 64", name="ck_login_session_digest"),
        CheckConstraint("length(csrf_digest) = 64", name="ck_login_session_csrf"),
        CheckConstraint("length(credential_digest) = 64", name="ck_login_session_credential"),
        CheckConstraint("expires_at > issued_at", name="ck_login_session_expiration"),
        Index("ix_login_sessions_user", "user_id", "expires_at"),
    )
    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    identity_id: Mapped[str] = mapped_column(ForeignKey("external_identities.id"))
    credential_generation: Mapped[int | None]
    credential_digest: Mapped[str] = mapped_column(String(64))
    csrf_digest: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
