from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base
from aris.modules.nova.schemas import ReservationStatus


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("start_at < end_at", name="ck_reservation_window"),
        CheckConstraint(
            "status IN ('pending','confirmed','active','completed',"
            "'cancelled','rejected','reclaiming')",
            name="ck_reservation_status",
        ),
        # SQLAlchemy 2.0's dialect constructor lacks a typed signature.
        ExcludeConstraint(  # type: ignore[no-untyped-call]
            ("gpu_uuid", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            name="ex_reservation_gpu_interval",
            using="gist",
            where=text("status IN ('confirmed','active','reclaiming')"),
            ops={"gpu_uuid": "public.gist_text_ops"},
        ).ddl_if(dialect="postgresql"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # The future service validates this stable opaque reference through Vega's
    # public contract, without importing/querying its private tables here.
    gpu_uuid: Mapped[str] = mapped_column(String(128), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ReservationStatus] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(256))
    policy_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReservationEvent(Base):
    __tablename__ = "reservation_events"
    __table_args__ = (
        UniqueConstraint("reservation_id", "version", name="uq_reservation_event_version"),
        CheckConstraint("version > 0", name="ck_reservation_event_version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PolicySourceVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("name", "source_version", name="uq_policy_source_version"),
        CheckConstraint("source_version > 0", name="ck_policy_source_version"),
        CheckConstraint("length(fingerprint) = 64", name="ck_policy_source_fingerprint"),
        CheckConstraint(
            "length(canonical_json) > 0 AND length(canonical_json) <= 65536",
            name="ck_policy_source_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer)
    canonical_json: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"))
    request_id: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyReviewMaterialRow(Base):
    __tablename__ = "policy_review_materials"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_policy_material_command"),
        CheckConstraint(
            "source_version>0 AND scenarios>=1 AND scenarios<=8", name="ck_policy_material_scope"
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(canonical_material)>0 AND length(canonical_material)<=65536",
            name="ck_policy_material_size",
        ),
        Index("ix_policy_material_source_time", "source_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    name: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    implementation_ref: Mapped[str] = mapped_column(String(128))
    scenarios: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"))
    request_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_material: Mapped[str] = mapped_column(Text)


class PolicyReviewFeedbackRow(Base):
    __tablename__ = "policy_review_feedback"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_policy_feedback_command"),
        CheckConstraint(
            "source_version>0 AND kind IN ('comment','changes_requested')",
            name="ck_policy_feedback_kind",
        ),
        CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(canonical_feedback)>0 AND length(canonical_feedback)<=16384",
            name="ck_policy_feedback_size",
        ),
        CheckConstraint(
            "(material_id IS NULL AND material_fingerprint IS NULL) OR "
            "(material_id IS NOT NULL AND material_fingerprint IS NOT NULL "
            "AND length(material_fingerprint)=64)",
            name="ck_policy_feedback_material",
        ),
        Index("ix_policy_feedback_source_time", "source_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("policy_versions.id"))
    name: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[int] = mapped_column(Integer)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    material_id: Mapped[str | None] = mapped_column(ForeignKey("policy_review_materials.id"))
    material_fingerprint: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"))
    request_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_feedback: Mapped[str] = mapped_column(Text)
