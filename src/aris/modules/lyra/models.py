from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class UsageWindowSnapshotRow(Base):
    __tablename__ = "usage_window_snapshots"
    __table_args__ = (
        Index("ix_usage_window_node_time", "node_id", "start_at", "end_at"),
        CheckConstraint("end_at>start_at", name="ck_usage_window_range"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_snapshot)>0 "
            "AND length(canonical_snapshot)<=8388608",
            name="ck_usage_window_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    node_id: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_snapshot: Mapped[str] = mapped_column(Text)


class UsageCoverageManifestRow(Base):
    __tablename__ = "usage_coverage_manifests"
    __table_args__ = (
        Index("ix_usage_coverage_source", "consumer_id", "attestation_id"),
        CheckConstraint("ledger_revision>=0 AND end_at>start_at", name="ck_usage_coverage_range"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(configuration_fingerprint)=64 "
            "AND length(canonical_manifest)>0 AND length(canonical_manifest)<=8388608",
            name="ck_usage_coverage_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    attestation_id: Mapped[str] = mapped_column(ForeignKey("usage_trace_attestations.id"))
    consumer_id: Mapped[str] = mapped_column(String(128))
    node_id: Mapped[str] = mapped_column(String(128))
    configuration_fingerprint: Mapped[str] = mapped_column(String(64))
    ledger_revision: Mapped[int] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_manifest: Mapped[str] = mapped_column(Text)


class UsageRecordingPipelineBatchRow(Base):
    __tablename__ = "usage_pipeline_batches"
    __table_args__ = (
        UniqueConstraint("consumer_id", "revision", name="uq_pipeline_batch_revision"),
        CheckConstraint("revision>0", name="ck_pipeline_batch_revision"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_batch)>0 "
            "AND length(canonical_batch)<=131072",
            name="ck_pipeline_batch_size",
        ),
    )
    id: Mapped[str] = mapped_column(ForeignKey("usage_trace_records.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    consumer_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(BigInteger)
    attestation_id: Mapped[str] = mapped_column(
        ForeignKey("usage_trace_attestations.id"), unique=True
    )
    segment_set_id: Mapped[str] = mapped_column(
        ForeignKey("usage_residency_segment_sets.id"), unique=True
    )
    canonical_batch: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageRecordingPlanRow(Base):
    __tablename__ = "usage_recording_plans"
    __table_args__ = (
        CheckConstraint(
            "partition_count>0 AND next_partition>=0 AND next_partition<=partition_count",
            name="ck_recording_plan_range",
        ),
        CheckConstraint(
            "version>=1 AND version<=next_partition+1 AND "
            "((version=1 AND next_partition=0) OR (version>1 AND next_partition>0))",
            name="ck_recording_plan_version",
        ),
        CheckConstraint("updated_at>=created_at", name="ck_recording_plan_time"),
        CheckConstraint(
            "length(fingerprint)=64 AND length(run_id)=64 AND "
            "length(canonical_plan)>0 AND length(canonical_plan)<=16384",
            name="ck_recording_plan_input",
        ),
        Index("ix_recording_plan_run", "consumer_id", "run_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    consumer_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[str] = mapped_column(String(64))
    source_record_id: Mapped[str] = mapped_column(ForeignKey("usage_trace_records.id"))
    closing_attestation_id: Mapped[str] = mapped_column(ForeignKey("usage_trace_attestations.id"))
    partition_count: Mapped[int] = mapped_column(BigInteger)
    canonical_plan: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_partition: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageResidencySegmentSetRow(Base):
    __tablename__ = "usage_residency_segment_sets"
    __table_args__ = (
        CheckConstraint(
            "segment_count >= 0 AND segment_count <= 16384", name="ck_residency_set_count"
        ),
        CheckConstraint("length(fingerprint)=64", name="ck_residency_set_fingerprint"),
        CheckConstraint(
            "length(canonical_record)>0 AND length(canonical_record)<=8388608",
            name="ck_residency_set_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    attestation_id: Mapped[str] = mapped_column(
        ForeignKey("usage_trace_attestations.id"), unique=True
    )
    consumer_id: Mapped[str] = mapped_column(String(128))
    segment_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_record: Mapped[str] = mapped_column(Text)


class UsageResidencySegmentRow(Base):
    __tablename__ = "usage_residency_segments"
    __table_args__ = (
        UniqueConstraint("set_id", "segment_index", name="uq_residency_segment_position"),
        UniqueConstraint(
            "consumer_id", "run_id", "interval_offset", name="uq_residency_segment_offset"
        ),
        CheckConstraint(
            "segment_index>=0 AND segment_index<16384", name="ck_residency_segment_position"
        ),
        CheckConstraint(
            "interval_offset>=0 AND through_interval>interval_offset "
            "AND through_interval-interval_offset<=128",
            name="ck_residency_segment_intervals",
        ),
        CheckConstraint("first_seq>0 AND last_seq>first_seq", name="ck_residency_segment_sources"),
        ExcludeConstraint(  # type: ignore[no-untyped-call]
            ("consumer_id", "="),
            ("run_id", "="),
            (text("int8range(interval_offset, through_interval, '[)')"), "&&"),
            name="ex_residency_segment_intervals",
            using="gist",
            ops={"consumer_id": "public.gist_text_ops", "run_id": "public.gist_text_ops"},
        ).ddl_if(dialect="postgresql"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    set_id: Mapped[str] = mapped_column(ForeignKey("usage_residency_segment_sets.id"))
    segment_index: Mapped[int] = mapped_column(Integer)
    consumer_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[str] = mapped_column(String(64))
    interval_offset: Mapped[int] = mapped_column(BigInteger)
    through_interval: Mapped[int] = mapped_column(BigInteger)
    first_seq: Mapped[int] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger)


class UsageRecordingJobRow(Base):
    __tablename__ = "usage_recording_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','completed','blocked','failed')",
            name="ck_recording_job_state",
        ),
        CheckConstraint(
            "version >= 1 AND attempts >= 0 AND attempts <= 20", name="ck_recording_job_counters"
        ),
        CheckConstraint(
            "length(request_fingerprint)=64 AND length(canonical_request)>0 "
            "AND length(canonical_request)<=16384",
            name="ck_recording_job_input",
        ),
        CheckConstraint(
            "updated_at>=created_at AND available_at>=created_at", name="ck_recording_job_times"
        ),
        CheckConstraint(
            "(state='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND attempts>0 AND lease_expires_at>updated_at) OR "
            "(state<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_recording_job_lease",
        ),
        CheckConstraint(
            "(state IN ('completed','blocked','failed') AND finished_at IS NOT NULL "
            "AND finished_at=updated_at) OR "
            "(state IN ('queued','running') AND finished_at IS NULL)",
            name="ck_recording_job_finish",
        ),
        CheckConstraint(
            "(state='completed' AND recording_id IS NOT NULL AND recording_id=id "
            "AND recording_fingerprint IS NOT NULL AND length(recording_fingerprint)=64) OR "
            "(state<>'completed' AND recording_id IS NULL AND recording_fingerprint IS NULL)",
            name="ck_recording_job_result",
        ),
        Index("ix_recording_jobs_ready", "state", "available_at", "lease_expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_request: Mapped[str] = mapped_column(Text)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recording_id: Mapped[str | None] = mapped_column(
        ForeignKey("usage_run_recordings.id"), unique=True
    )
    recording_fingerprint: Mapped[str | None] = mapped_column(String(64))


class UsageTraceAttestationRow(Base):
    __tablename__ = "usage_trace_attestations"
    __table_args__ = (
        UniqueConstraint("consumer_id", "revision", name="uq_usage_trace_attestation_revision"),
        CheckConstraint("revision > 0", name="ck_usage_trace_attestation_revision"),
        CheckConstraint(
            "first_seq > 0 AND last_seq >= first_seq AND last_seq - first_seq < 128",
            name="ck_usage_trace_attestation_range",
        ),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_trace_attestation_fingerprint"),
        CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 16384",
            name="ck_usage_trace_attestation_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("usage_trace_records.id"), unique=True)
    previous_id: Mapped[str | None] = mapped_column(ForeignKey("usage_trace_attestations.id"))
    consumer_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(BigInteger)
    first_seq: Mapped[int] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_record: Mapped[str] = mapped_column(Text)


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
            "length(canonical_record) > 0 AND length(canonical_record) <= 1048576",
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


class ClosedRunRecordingRow(Base):
    __tablename__ = "usage_run_recordings"
    __table_args__ = (
        Index("ix_usage_run_recording_node_run", "node_id", "run_id"),
        CheckConstraint("length(fingerprint) = 64", name="ck_usage_run_recording_fingerprint"),
        CheckConstraint("length(run_id) = 64", name="ck_usage_run_recording_run"),
        CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 524288",
            name="ck_usage_run_recording_size",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.event_id"), unique=True)
    derivation_id: Mapped[str] = mapped_column(ForeignKey("usage_derivations.id"), unique=True)
    ledger_operation_id: Mapped[str] = mapped_column(
        ForeignKey("usage_ledger_operations.id"), unique=True
    )
    node_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    canonical_record: Mapped[str] = mapped_column(Text)
