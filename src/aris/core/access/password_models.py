"""Private password and one-time invitation state; no plaintext authentication secrets."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class PasswordAccount(Base):
    __tablename__ = "user_password_accounts"
    __table_args__ = (
        CheckConstraint("challenge_version >= 0", name="ck_password_challenge_version"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    challenge_version: Mapped[int] = mapped_column(BigInteger, default=0)


class LoginInvitation(Base):
    __tablename__ = "user_login_invitations"
    __table_args__ = (
        CheckConstraint("purpose IN ('activate','reset')", name="ck_login_invitation_purpose"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_login_invitation_attempts"),
        CheckConstraint("length(code_digest) = 64", name="ck_login_invitation_digest"),
        CheckConstraint("expires_at > created_at", name="ck_login_invitation_expiration"),
        Index("ix_login_invitations_user_created", "user_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    identity_id: Mapped[str] = mapped_column(ForeignKey("external_identities.id"))
    username: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    code_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginRateWindow(Base):
    __tablename__ = "login_rate_windows"
    __table_args__ = (CheckConstraint("attempts >= 0", name="ck_login_rate_attempts"),)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int]
