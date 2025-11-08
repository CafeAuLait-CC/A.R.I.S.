from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Boolean
)
from sqlalchemy.orm import relationship
from datetime import datetime

from ...core.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    slack_id = Column(String, unique=True, index=True)
    display_name = Column(String)
    role = Column(String, default="member")
    created_at = Column(DateTime, default=datetime.utcnow)


class GpuNode(Base):
    __tablename__ = "gpu_nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, index=True)
    token = Column(String, unique=True, index=True)  # for agent authentication
    description = Column(String, nullable=True)
    last_report = Column(DateTime)


class GpuStatus(Base):
    __tablename__ = "gpu_status"

    id = Column(Integer, primary_key=True)
    node_id = Column(Integer, ForeignKey("gpu_nodes.id"))
    gpu_index = Column(Integer)
    username = Column(String)
    process_count = Column(Integer)
    memory_used_mb = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)

    node = relationship("GpuNode")


class GpuReservation(Base):
    __tablename__ = "gpu_reservations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    gpu = Column(String)  # 如 "node1:0" 或 "A6000-1"
    start_time = Column(DateTime)
    end_time = Column(DateTime, nullable=True)
    status = Column(String, default="active")  # active / finished / cancelled

    user = relationship("User")
