"""Immutable local residency segment sets and original interval indexes."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_residency_segment_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "attestation_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_attestations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("segment_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "segment_count >= 0 AND segment_count <= 16384", name="ck_residency_set_count"
        ),
        sa.CheckConstraint("length(fingerprint)=64", name="ck_residency_set_fingerprint"),
        sa.CheckConstraint(
            "length(canonical_record)>0 AND length(canonical_record)<=8388608",
            name="ck_residency_set_size",
        ),
    )
    op.create_table(
        "usage_residency_segments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "set_id",
            sa.String(36),
            sa.ForeignKey("usage_residency_segment_sets.id"),
            nullable=False,
        ),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("interval_offset", sa.BigInteger(), nullable=False),
        sa.Column("through_interval", sa.BigInteger(), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("set_id", "segment_index", name="uq_residency_segment_position"),
        sa.UniqueConstraint(
            "consumer_id", "run_id", "interval_offset", name="uq_residency_segment_offset"
        ),
        sa.CheckConstraint(
            "segment_index>=0 AND segment_index<16384", name="ck_residency_segment_position"
        ),
        sa.CheckConstraint(
            "interval_offset>=0 AND through_interval>interval_offset "
            "AND through_interval-interval_offset<=128",
            name="ck_residency_segment_intervals",
        ),
        sa.CheckConstraint(
            "first_seq>0 AND last_seq>first_seq", name="ck_residency_segment_sources"
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE usage_residency_segments ADD CONSTRAINT ex_residency_segment_intervals "
            "EXCLUDE USING gist (consumer_id public.gist_text_ops WITH =, "
            "run_id public.gist_text_ops WITH =, "
            "int8range(interval_offset,through_interval,'[)') WITH &&)"
        )
        op.execute(
            "CREATE FUNCTION aris_residency_segments_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'residency segment history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        for table in ("usage_residency_segment_sets", "usage_residency_segments"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION aris_residency_segments_immutable()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION aris_residency_segments_immutable()"
            )
    elif dialect == "sqlite":
        for table in ("usage_residency_segment_sets", "usage_residency_segments"):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT "
                    "RAISE(ABORT,'residency segment history is immutable'); END"
                )
    else:
        raise RuntimeError("unsupported residency segment database")


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("usage_residency_segments", "usage_residency_segment_sets"):
        if connection.scalar(sa.select(sa.func.count()).select_from(sa.table(table))):
            raise RuntimeError("residency segment history prevents downgrade")
    op.drop_table("usage_residency_segments")
    op.drop_table("usage_residency_segment_sets")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_residency_segments_immutable()")
