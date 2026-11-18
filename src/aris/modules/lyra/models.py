from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
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


class UsageLedgerEntryRow(Base):
    __tablename__ = "usage_ledger_entries"
    __table_args__ = (
        UniqueConstraint("node_id", "revision", name="uq_usage_ledger_revision"),
        UniqueConstraint("id", "kind", name="uq_usage_ledger_entry_kind"),
        ForeignKeyConstraint(
            ("target_id", "target_kind"),
            ("usage_ledger_entries.id", "usage_ledger_entries.kind"),
            name="fk_usage_reversal_measurement",
        ),
        CheckConstraint("revision > 0", name="ck_usage_ledger_revision"),
        CheckConstraint("start_at < end_at AND recorded_at >= end_at", name="ck_usage_ledger_time"),
        CheckConstraint("observed_microseconds > 0", name="ck_usage_ledger_duration"),
        CheckConstraint(
            "(kind = 'measurement' AND measurement_key IS NOT NULL AND target_id IS NULL "
            "AND target_kind IS NULL) OR (kind = 'reversal' AND measurement_key IS NULL "
            "AND target_id IS NOT NULL AND target_kind IS NOT NULL "
            "AND target_kind = 'measurement' AND target_id != id)",
            name="ck_usage_ledger_kind",
        ),
        CheckConstraint(
            "(revision = 1 AND previous_fingerprint IS NULL) OR "
            "(revision > 1 AND previous_fingerprint IS NOT NULL "
            "AND length(previous_fingerprint) = 64)",
            name="ck_usage_ledger_previous",
        ),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_ledger_fingerprint"),
        CheckConstraint(
            "length(canonical_entry) > 0 AND length(canonical_entry) <= 65536",
            name="ck_usage_ledger_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    gpu_uuid: Mapped[str] = mapped_column(String(128), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    revision: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    measurement_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    target_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    target_kind: Mapped[str | None] = mapped_column(String(16))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_microseconds: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    previous_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_entry: Mapped[str] = mapped_column(Text)


class UsageLedgerClaim(Base):
    """Rebuildable exclusion projection; the entry itself remains immutable."""

    __tablename__ = "usage_ledger_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ("entry_id", "entry_kind"),
            ("usage_ledger_entries.id", "usage_ledger_entries.kind"),
            name="fk_usage_claim_measurement",
        ),
        CheckConstraint("entry_kind = 'measurement'", name="ck_usage_claim_kind"),
        CheckConstraint("start_at < end_at", name="ck_usage_claim_time"),
        ExcludeConstraint(  # type: ignore[no-untyped-call]
            ("gpu_uuid", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            name="ex_usage_ledger_gpu_interval",
            using="gist",
            ops={"gpu_uuid": "public.gist_text_ops"},
        ).ddl_if(dialect="postgresql"),
    )
    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    entry_kind: Mapped[str] = mapped_column(String(16))
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    gpu_uuid: Mapped[str] = mapped_column(String(128), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageLedgerOperationRow(Base):
    __tablename__ = "usage_ledger_operations"
    __table_args__ = (
        CheckConstraint(
            "length(request_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_usage_operation_fingerprints",
        ),
        CheckConstraint(
            "length(canonical_request) > 0 AND length(canonical_request) <= 65536 "
            "AND length(canonical_result) > 0 AND length(canonical_result) <= 65536",
            name="ck_usage_operation_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    result_fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_request: Mapped[str] = mapped_column(Text)
    canonical_result: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageLedgerState(Base):
    __tablename__ = "usage_ledger_state"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_usage_state_revision"),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_state_fingerprint"),
    )
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))


class UsageTraceRecordRow(Base):
    __tablename__ = "usage_trace_records"
    __table_args__ = (
        UniqueConstraint("consumer_id", "revision", name="uq_usage_trace_revision"),
        CheckConstraint("revision > 0 AND after_seq > 0", name="ck_usage_trace_progress"),
        CheckConstraint("source_count >= 1 AND source_count <= 128", name="ck_usage_trace_sources"),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_trace_fingerprint"),
        CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 262144",
            name="ck_usage_trace_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    consumer_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(BigInteger)
    after_seq: Mapped[int] = mapped_column(BigInteger)
    source_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_record: Mapped[str] = mapped_column(Text)


class UsageTraceCursorRow(Base):
    __tablename__ = "usage_trace_cursors"
    __table_args__ = (
        CheckConstraint("revision > 0 AND after_seq > 0", name="ck_usage_trace_cursor_progress"),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_trace_cursor_fingerprint"),
    )
    consumer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("usage_trace_records.id"), unique=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    after_seq: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
