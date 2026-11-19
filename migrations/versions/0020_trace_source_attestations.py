"""Immutable bounded original-source attestation segments."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_trace_attestations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "record_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_records.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("previous_id", sa.String(36), sa.ForeignKey("usage_trace_attestations.id")),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.UniqueConstraint("consumer_id", "revision", name="uq_usage_trace_attestation_revision"),
        sa.CheckConstraint("revision > 0", name="ck_usage_trace_attestation_revision"),
        sa.CheckConstraint(
            "first_seq > 0 AND last_seq >= first_seq AND last_seq - first_seq < 128",
            name="ck_usage_trace_attestation_range",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64", name="ck_usage_trace_attestation_fingerprint"
        ),
        sa.CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 16384",
            name="ck_usage_trace_attestation_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_usage_trace_attestation_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
            "'source attestation history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER usage_trace_attestations_immutable BEFORE UPDATE OR DELETE "
            "ON usage_trace_attestations FOR EACH ROW "
            "EXECUTE FUNCTION aris_usage_trace_attestation_immutable()"
        )
        op.execute(
            "CREATE TRIGGER usage_trace_attestations_no_truncate BEFORE TRUNCATE "
            "ON usage_trace_attestations FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_usage_trace_attestation_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER usage_trace_attestations_no_{operation.lower()} "
                f"BEFORE {operation} "
                "ON usage_trace_attestations BEGIN SELECT "
                "RAISE(ABORT,'source attestation history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported source attestation database")


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_trace_attestations"))
    )
    if count:
        raise RuntimeError("source attestation history prevents downgrade")
    op.drop_table("usage_trace_attestations")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_usage_trace_attestation_immutable()")
