from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class Node(Base):
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), unique=True)
    hostname: Mapped[str] = mapped_column(String(256))
    agent_version: Mapped[str] = mapped_column(String(64))
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # observed_at/received_at are liveness; GPU facts have independent freshness.
    telemetry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telemetry_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telemetry_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    collector_status: Mapped[Literal["ok", "error", "unknown"]] = mapped_column(
        String(16), server_default="unknown"
    )
    collector_health_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    collector_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeResource(Base):
    __tablename__ = "node_resources"
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation: Mapped[dict[str, Any]] = mapped_column(JSON)


class GpuDevice(Base):
    __tablename__ = "gpu_devices"
    gpu_uuid: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(256))
    memory_total_mib: Mapped[int] = mapped_column(Integer)
    memory_used_mib: Mapped[int] = mapped_column(Integer)
    utilization_percent: Mapped[int] = mapped_column(Integer)
    temperature_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    present: Mapped[bool] = mapped_column(Boolean)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("agent_id", "message_id", name="uq_telemetry_message"),
        UniqueConstraint("agent_id", "seq", name="uq_telemetry_sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36))
    agent_id: Mapped[str] = mapped_column(String(128))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    message_kind: Mapped[str] = mapped_column(String(16), server_default="telemetry")
    fingerprint: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    compute_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_to_current: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class AgentMessageReceipt(Base):
    __tablename__ = "agent_message_receipts"
    __table_args__ = (
        UniqueConstraint("agent_id", "message_id", name="uq_agent_message_receipt_id"),
        CheckConstraint(
            "seq > 0 AND length(fingerprint)=64",
            name="ck_agent_message_receipt_identity",
        ),
    )
    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36))
    fingerprint: Mapped[str] = mapped_column(String(64))
    applied_to_current: Mapped[bool] = mapped_column(Boolean)
