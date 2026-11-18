"""Immutable bindings of verified closed runs to measured ledger operations."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_run_recordings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "derivation_id",
            sa.String(36),
            sa.ForeignKey("usage_derivations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "ledger_operation_id",
            sa.String(36),
            sa.ForeignKey("usage_ledger_operations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_usage_run_recording_fingerprint"),
        sa.CheckConstraint("length(run_id) = 64", name="ck_usage_run_recording_run"),
        sa.CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 524288",
            name="ck_usage_run_recording_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_usage_run_recording_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'usage run recording history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER usage_run_recordings_immutable BEFORE UPDATE OR DELETE "
            "ON usage_run_recordings FOR EACH ROW "
            "EXECUTE FUNCTION aris_usage_run_recording_immutable()"
        )
        op.execute(
            "CREATE TRIGGER usage_run_recordings_no_truncate BEFORE TRUNCATE "
            "ON usage_run_recordings FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_usage_run_recording_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER usage_run_recordings_no_{operation.lower()} BEFORE {operation} "
                "ON usage_run_recordings BEGIN SELECT "
                "RAISE(ABORT,'usage run recording history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported usage run recording database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(sa.table("usage_run_recordings"))):
        raise RuntimeError("usage run recording history prevents downgrade")
    op.drop_table("usage_run_recordings")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_usage_run_recording_immutable()")
