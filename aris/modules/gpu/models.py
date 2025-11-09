import enum
import uuid
import datetime as dt

from sqlalchemy import (
    String,
    Integer,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...core.db import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


# ===== User =====


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)

    name: Mapped[str] = mapped_column(String(128), default="Unknown User")

    # UBC CWL (Campus-Wide-Login)
    cwl: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # username on GPU server
    gpu_node_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    slack_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slack_display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole), default=UserRole.MEMBER, nullable=False
    )

    # GPU Weekly Usage Quota, 100 hours by default
    weekly_quota_minutes: Mapped[int] = mapped_column(
        Integer, default=100 * 60, nullable=False
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_users_gpu_node_username", "gpu_node_username"),
        Index("ix_users_slack_uid", "slack_uid"),
        Index("ix_users_cwl", "cwl"),
    )


# ===== GPU Node / GPU =====


class GpuNode(Base):
    __tablename__ = "gpu_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)

    # GPU server host name
    hostname: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    agent_version = mapped_column(String(32), nullable=True)

    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        nullable=False,
    )

    gpus: Mapped[list["Gpu"]] = relationship("Gpu", back_populates="node")


class Gpu(Base):
    __tablename__ = "gpus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)

    # server id
    node_id: Mapped[str] = mapped_column(ForeignKey("gpu_nodes.id"), nullable=False)

    # GPU Number
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    uuid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # GPU Model
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    node: Mapped[GpuNode] = relationship("GpuNode", back_populates="gpus")

    __table_args__ = (UniqueConstraint("node_id", "index", name="uq_gpu_node_index"),)


# ===== Session / UsageLog =====


class SessionState(str, enum.Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    ENDED = "ended"


class UsageTag(str, enum.Enum):
    NORMAL = "normal"
    RESERVED = "reserved"
    PENALTY = "penalty"
    COMPENSATION = "compensation"


class GpuSession(Base):
    __tablename__ = "gpu_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    gpu_id: Mapped[str] = mapped_column(ForeignKey("gpus.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(ForeignKey("gpu_nodes.id"), nullable=False)

    state: Mapped[SessionState] = mapped_column(Enum(SessionState), nullable=False)
    # reservation related（optional）
    reserved_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reserved_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # agent/manual/slack
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship()
    gpu: Mapped[Gpu] = relationship()
    node: Mapped[GpuNode] = relationship()

    __table_args__ = (
        Index("ix_gpu_sessions_user_id", "user_id"),
        Index("ix_gpu_sessions_gpu_id", "gpu_id"),
        Index("ix_gpu_sessions_state", "state"),
    )


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    gpu_id: Mapped[str] = mapped_column(ForeignKey("gpus.id"), nullable=False)
    node_id: Mapped[str] = mapped_column(ForeignKey("gpu_nodes.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("gpu_sessions.id"))

    start_ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    tag: Mapped[UsageTag | None] = mapped_column(Enum(UsageTag), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_usage_logs_user_id", "user_id"),
        Index("ix_usage_logs_gpu_id", "gpu_id"),
        Index("ix_usage_logs_start_ts", "start_ts"),
    )
