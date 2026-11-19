"""Atomic source preparation manifests are retained separately from usage coverage."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_pipeline_batches",
        sa.Column("id", sa.String(36), sa.ForeignKey("usage_trace_records.id"), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "attestation_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_attestations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "segment_set_id",
            sa.String(36),
            sa.ForeignKey("usage_residency_segment_sets.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("canonical_batch", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("consumer_id", "revision", name="uq_pipeline_batch_revision"),
        sa.CheckConstraint("revision>0", name="ck_pipeline_batch_revision"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_batch)>0 "
            "AND length(canonical_batch)<=131072",
            name="ck_pipeline_batch_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_pipeline_batches_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'pipeline batch history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER pipeline_batches_immutable BEFORE UPDATE OR DELETE "
            "ON usage_pipeline_batches "
            "FOR EACH ROW EXECUTE FUNCTION aris_pipeline_batches_immutable()"
        )
        op.execute(
            "CREATE TRIGGER pipeline_batches_no_truncate BEFORE TRUNCATE ON usage_pipeline_batches "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_pipeline_batches_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER pipeline_batches_no_{operation.lower()} BEFORE {operation} "
                "ON usage_pipeline_batches BEGIN SELECT "
                "RAISE(ABORT,'pipeline batch history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported pipeline batch database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_pipeline_batches"))
    ):
        raise RuntimeError("pipeline batch history prevents downgrade")
    op.drop_table("usage_pipeline_batches")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_pipeline_batches_immutable()")
