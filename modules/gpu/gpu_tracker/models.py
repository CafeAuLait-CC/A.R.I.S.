from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime, Boolean, ForeignKey, Enum, Text
import enum, datetime as dt

Base = declarative_base()

class SessionState(str, enum.Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    ENDED = "ended"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    weekly_quota_minutes: Mapped[int] = mapped_column(Integer, default=100*60)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class GPU(Base):
    __tablename__ = "gpus"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)

class GPUSession(Base):
    __tablename__ = "gpu_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id"))
    state: Mapped[str] = mapped_column(Enum(SessionState))
    reserved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reserve_minutes: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

class UsageLog(Base):
    __tablename__ = "usage_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id"))
    start_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    minutes: Mapped[int] = mapped_column(Integer)
    tag: Mapped[str | None] = mapped_column(String, nullable=True)
