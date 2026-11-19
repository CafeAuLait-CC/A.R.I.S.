"""Retain immutable source processing coverage without enabling quota metrics."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_coverage_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            unique=True,
            nullable=False,
        ),
        sa.Column(
            "attestation_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_attestations.id"),
            nullable=False,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("ledger_revision", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_manifest", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "ledger_revision>=0 AND end_at>start_at", name="ck_usage_coverage_range"
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(configuration_fingerprint)=64 "
            "AND length(canonical_manifest)>0 AND length(canonical_manifest)<=8388608",
            name="ck_usage_coverage_size",
        ),
    )
    op.create_index(
        "ix_usage_coverage_source", "usage_coverage_manifests", ["consumer_id", "attestation_id"]
    )
    op.create_index(
        "ix_usage_run_recording_node_run", "usage_run_recordings", ["node_id", "run_id"]
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_coverage_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'processing coverage history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER coverage_immutable BEFORE UPDATE OR DELETE ON usage_coverage_manifests "
            "FOR EACH ROW EXECUTE FUNCTION aris_coverage_immutable()"
        )
        op.execute(
            "CREATE TRIGGER coverage_no_truncate BEFORE TRUNCATE ON usage_coverage_manifests "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_coverage_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER coverage_no_{operation.lower()} BEFORE {operation} "
                "ON usage_coverage_manifests BEGIN SELECT "
                "RAISE(ABORT,'processing coverage history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported coverage manifest database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_coverage_manifests"))
    ):
        raise RuntimeError("processing coverage history prevents downgrade")
    op.drop_index("ix_usage_run_recording_node_run", table_name="usage_run_recordings")
    op.drop_table("usage_coverage_manifests")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_coverage_immutable()")
