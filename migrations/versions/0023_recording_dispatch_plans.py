"""Immutable closed-parent plans with bounded transactional dispatch progress."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_FIXED = (
    "id",
    "event_id",
    "consumer_id",
    "run_id",
    "source_record_id",
    "closing_attestation_id",
    "partition_count",
    "canonical_plan",
    "fingerprint",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "usage_recording_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column(
            "source_record_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_records.id"),
            nullable=False,
        ),
        sa.Column(
            "closing_attestation_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_attestations.id"),
            nullable=False,
        ),
        sa.Column("partition_count", sa.BigInteger(), nullable=False),
        sa.Column("canonical_plan", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_partition", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "partition_count>0 AND next_partition>=0 AND next_partition<=partition_count",
            name="ck_recording_plan_range",
        ),
        sa.CheckConstraint(
            "version>=1 AND version<=next_partition+1 AND "
            "((version=1 AND next_partition=0) OR (version>1 AND next_partition>0))",
            name="ck_recording_plan_version",
        ),
        sa.CheckConstraint("updated_at>=created_at", name="ck_recording_plan_time"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(run_id)=64 AND "
            "length(canonical_plan)>0 AND length(canonical_plan)<=16384",
            name="ck_recording_plan_input",
        ),
    )
    op.create_index("ix_recording_plan_run", "usage_recording_plans", ["consumer_id", "run_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        changed = " OR ".join(f"NEW.{key} IS DISTINCT FROM OLD.{key}" for key in _FIXED)
        op.execute(
            "CREATE FUNCTION aris_recording_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF TG_OP='DELETE' OR TG_OP='TRUNCATE' THEN "
            "RAISE EXCEPTION 'recording plan history is retained' USING ERRCODE='23000'; END IF; "
            f"IF {changed} THEN RAISE EXCEPTION 'recording plan input is immutable' "
            "USING ERRCODE='23000'; END IF; "
            "IF (NEW.next_partition IS DISTINCT FROM OLD.next_partition "
            "OR NEW.version IS DISTINCT FROM OLD.version "
            "OR NEW.updated_at IS DISTINCT FROM OLD.updated_at) AND NOT "
            "(NEW.next_partition>OLD.next_partition AND NEW.next_partition<=OLD.next_partition+8 "
            "AND NEW.version=OLD.version+1 AND NEW.updated_at>=OLD.updated_at) THEN "
            "RAISE EXCEPTION 'recording plan progress must advance in bounded batches' "
            "USING ERRCODE='23000'; END IF; "
            "RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER recording_plan_guard BEFORE UPDATE OR DELETE ON usage_recording_plans "
            "FOR EACH ROW EXECUTE FUNCTION aris_recording_plan_guard()"
        )
        op.execute(
            "CREATE TRIGGER recording_plans_no_truncate BEFORE TRUNCATE ON usage_recording_plans "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_recording_plan_guard()"
        )
    elif dialect == "sqlite":
        changed = " OR ".join(f"NEW.{key} IS NOT OLD.{key}" for key in _FIXED)
        op.execute(
            "CREATE TRIGGER recording_plans_no_delete BEFORE DELETE ON usage_recording_plans "
            "BEGIN SELECT RAISE(ABORT,'recording plan history is retained'); END"
        )
        op.execute(
            "CREATE TRIGGER recording_plans_input_guard BEFORE UPDATE ON usage_recording_plans "
            f"WHEN {changed} BEGIN SELECT RAISE(ABORT,'recording plan input is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER recording_plans_progress_guard BEFORE UPDATE ON usage_recording_plans "
            "WHEN (NEW.next_partition IS NOT OLD.next_partition OR NEW.version IS NOT OLD.version "
            "OR NEW.updated_at IS NOT OLD.updated_at) AND NOT "
            "(NEW.next_partition>OLD.next_partition AND NEW.next_partition<=OLD.next_partition+8 "
            "AND NEW.version=OLD.version+1 AND NEW.updated_at>=OLD.updated_at) "
            "BEGIN SELECT RAISE(ABORT,'recording plan progress must advance in bounded batches'); "
            "END"
        )
    else:
        raise RuntimeError("unsupported recording plan database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(sa.table("usage_recording_plans"))):
        raise RuntimeError("recording plan history prevents downgrade")
    op.drop_table("usage_recording_plans")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_recording_plan_guard()")
