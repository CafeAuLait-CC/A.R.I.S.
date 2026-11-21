"""Bounded previously verified original-ledger segments, without a second ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None
_TABLE = "usage_ledger_segments"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("previous_id", sa.String(36), sa.ForeignKey(_TABLE + ".id")),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("first_revision", sa.BigInteger(), nullable=False),
        sa.Column("last_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "tail_entry_id", sa.String(36), sa.ForeignKey("usage_ledger_entries.id"), nullable=False
        ),
        sa.Column("minimum_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("maximum_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_segment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_id", "last_revision", name="uq_ledger_segment_end"),
        sa.CheckConstraint(
            "first_revision > 0 AND last_revision >= first_revision AND "
            "last_revision-first_revision < 256",
            name="ck_ledger_segment_revisions",
        ),
        sa.CheckConstraint("minimum_start < maximum_end", name="ck_ledger_segment_time"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(configuration_fingerprint)=64",
            name="ck_ledger_segment_fingerprints",
        ),
        sa.CheckConstraint(
            "length(canonical_segment)>0 AND length(canonical_segment)<=262144",
            name="ck_ledger_segment_size",
        ),
    )
    op.create_index("ix_ledger_segment_scope", _TABLE, ["node_id", "minimum_start", "maximum_end"])
    op.create_index(
        "ix_ledger_original_overlap",
        "usage_ledger_entries",
        ["node_id", "gpu_uuid", "end_at", "start_at", "revision"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_ledger_segment_immutable() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN RAISE EXCEPTION 'ledger segment history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER ledger_segment_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_ledger_segment_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER ledger_segment_no_truncate BEFORE TRUNCATE ON {_TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_ledger_segment_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER ledger_segment_{operation.lower()} BEFORE {operation} ON "
                f"{_TABLE} BEGIN SELECT RAISE(ABORT,'ledger segment history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported ledger segment database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(_TABLE))):
        raise RuntimeError("ledger segment history prevents downgrade")
    op.drop_table(_TABLE)
    op.drop_index("ix_ledger_original_overlap", table_name="usage_ledger_entries")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_ledger_segment_immutable()")
