"""Immutable candidate processing records and a rebuildable current cursor."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_trace_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("after_seq", sa.BigInteger(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.UniqueConstraint("consumer_id", "revision", name="uq_usage_trace_revision"),
        sa.CheckConstraint("revision > 0 AND after_seq > 0", name="ck_usage_trace_progress"),
        sa.CheckConstraint(
            "source_count >= 1 AND source_count <= 128", name="ck_usage_trace_sources"
        ),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_usage_trace_fingerprint"),
        sa.CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 262144",
            name="ck_usage_trace_size",
        ),
    )
    op.create_table(
        "usage_trace_cursors",
        sa.Column("consumer_id", sa.String(128), primary_key=True),
        sa.Column(
            "record_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_records.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("after_seq", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("revision > 0 AND after_seq > 0", name="ck_usage_trace_cursor_progress"),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_usage_trace_cursor_fingerprint"),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_usage_trace_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'usage candidate trace history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER usage_trace_records_immutable BEFORE UPDATE OR DELETE "
            "ON usage_trace_records "
            "FOR EACH ROW EXECUTE FUNCTION aris_usage_trace_immutable()"
        )
        op.execute(
            "CREATE TRIGGER usage_trace_records_no_truncate BEFORE TRUNCATE ON usage_trace_records "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_usage_trace_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER usage_trace_records_no_{operation.lower()} BEFORE {operation} "
                "ON usage_trace_records BEGIN SELECT "
                "RAISE(ABORT,'usage candidate trace history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported usage trace database")


def downgrade() -> None:
    connection = op.get_bind()
    tables = ("usage_trace_cursors", "usage_trace_records")
    if any(
        connection.scalar(sa.select(sa.func.count()).select_from(sa.table(name))) for name in tables
    ):
        raise RuntimeError("usage candidate trace history prevents downgrade")
    for table in tables:
        op.drop_table(table)
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_usage_trace_immutable()")
