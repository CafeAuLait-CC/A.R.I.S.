"""Immutable measured-time chain and rebuildable active-interval projection."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_ledger_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("gpu_uuid", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("measurement_key", sa.String(64), nullable=True, unique=True),
        sa.Column("target_id", sa.String(36), nullable=True, unique=True),
        sa.Column("target_kind", sa.String(16), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_microseconds", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_fingerprint", sa.String(64), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_entry", sa.Text(), nullable=False),
        sa.UniqueConstraint("node_id", "revision", name="uq_usage_ledger_revision"),
        sa.UniqueConstraint("id", "kind", name="uq_usage_ledger_entry_kind"),
        sa.ForeignKeyConstraint(
            ("target_id", "target_kind"),
            ("usage_ledger_entries.id", "usage_ledger_entries.kind"),
            name="fk_usage_reversal_measurement",
        ),
        sa.CheckConstraint("revision > 0", name="ck_usage_ledger_revision"),
        sa.CheckConstraint(
            "start_at < end_at AND recorded_at >= end_at", name="ck_usage_ledger_time"
        ),
        sa.CheckConstraint("observed_microseconds > 0", name="ck_usage_ledger_duration"),
        sa.CheckConstraint(
            "(kind = 'measurement' AND measurement_key IS NOT NULL AND target_id IS NULL "
            "AND target_kind IS NULL) OR (kind = 'reversal' AND measurement_key IS NULL "
            "AND target_id IS NOT NULL AND target_kind IS NOT NULL "
            "AND target_kind = 'measurement' AND target_id != id)",
            name="ck_usage_ledger_kind",
        ),
        sa.CheckConstraint(
            "(revision = 1 AND previous_fingerprint IS NULL) OR "
            "(revision > 1 AND previous_fingerprint IS NOT NULL "
            "AND length(previous_fingerprint) = 64)",
            name="ck_usage_ledger_previous",
        ),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_usage_ledger_fingerprint"),
        sa.CheckConstraint(
            "length(canonical_entry) > 0 AND length(canonical_entry) <= 65536",
            name="ck_usage_ledger_size",
        ),
    )
    for column in ("node_id", "gpu_uuid"):
        op.create_index("ix_usage_ledger_entries_" + column, "usage_ledger_entries", [column])
    op.create_table(
        "usage_ledger_claims",
        sa.Column("entry_id", sa.String(36), primary_key=True),
        sa.Column("entry_kind", sa.String(16), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("gpu_uuid", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ("entry_id", "entry_kind"),
            ("usage_ledger_entries.id", "usage_ledger_entries.kind"),
            name="fk_usage_claim_measurement",
        ),
        sa.CheckConstraint("entry_kind = 'measurement'", name="ck_usage_claim_kind"),
        sa.CheckConstraint("start_at < end_at", name="ck_usage_claim_time"),
    )
    for column in ("node_id", "gpu_uuid"):
        op.create_index("ix_usage_ledger_claims_" + column, "usage_ledger_claims", [column])
    op.create_table(
        "usage_ledger_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("result_fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("canonical_result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_usage_operation_fingerprints",
        ),
        sa.CheckConstraint(
            "length(canonical_request) > 0 AND length(canonical_request) <= 65536 "
            "AND length(canonical_result) > 0 AND length(canonical_result) <= 65536",
            name="ck_usage_operation_size",
        ),
    )
    op.create_table(
        "usage_ledger_state",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_usage_state_revision"),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_usage_state_fingerprint"),
    )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE usage_ledger_claims ADD CONSTRAINT ex_usage_ledger_gpu_interval "
            "EXCLUDE USING gist (gpu_uuid public.gist_text_ops WITH =, "
            "tstzrange(start_at, end_at, '[)') WITH &&)"
        )
        op.execute(
            "CREATE FUNCTION aris_usage_ledger_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'usage ledger history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        for table in ("usage_ledger_entries", "usage_ledger_operations"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION aris_usage_ledger_immutable()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION aris_usage_ledger_immutable()"
            )
    elif connection.dialect.name == "sqlite":
        for table in ("usage_ledger_entries", "usage_ledger_operations"):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT,'usage ledger history is immutable'); END"
                )
    else:
        raise RuntimeError("unsupported usage ledger database")


def downgrade() -> None:
    connection = op.get_bind()
    tables = (
        "usage_ledger_claims",
        "usage_ledger_operations",
        "usage_ledger_state",
        "usage_ledger_entries",
    )
    if any(
        connection.scalar(sa.select(sa.func.count()).select_from(sa.table(name))) for name in tables
    ):
        raise RuntimeError("usage ledger history prevents downgrade")
    for name in tables:
        op.drop_table(name)
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_usage_ledger_immutable()")
