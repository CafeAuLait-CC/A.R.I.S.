"""Durable registered database jobs and their immutable attempt history."""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

JOB_DEFINITION = (
    "id",
    "job_key",
    "kind",
    "owner_id",
    "scope_type",
    "scope_id",
    "scheduled_for",
    "created_at",
    "canonical_request",
    "fingerprint",
)
RUN_DEFINITION = ("id", "job_id", "attempt", "lease_token", "started_at", "lease_expires_at")


def upgrade() -> None:
    op.create_table(
        "usage_weekly_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=True),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_microseconds", sa.BigInteger(), nullable=False),
        sa.Column("ledger_revision", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_report", sa.Text(), nullable=False),
        sa.CheckConstraint("end_at>start_at AND created_at>=end_at", name="ck_usage_weekly_period"),
        sa.CheckConstraint(
            "recorded_microseconds>=0 AND ledger_revision>=0", name="ck_usage_weekly_totals"
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_report)>0 "
            "AND length(canonical_report)<=4194304",
            name="ck_usage_weekly_size",
        ),
    )
    op.create_index(
        "ix_usage_weekly_node_period", "usage_weekly_reports", ["node_id", "start_at", "end_at"]
    )
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_key", sa.String(512), nullable=False, unique=True),
        sa.Column("kind", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_id", sa.String(36), nullable=True),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canonical_result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','running','retry','completed','blocked','dead')",
            name="ck_scheduled_job_state",
        ),
        sa.CheckConstraint(
            "attempts>=0 AND attempts<=10 AND updated_at>=created_at AND available_at>=created_at",
            name="ck_scheduled_job_bounds",
        ),
        sa.CheckConstraint(
            "((state='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL))",
            name="ck_scheduled_job_lease",
        ),
        sa.CheckConstraint(
            "((state='completed' AND canonical_result IS NOT NULL) "
            "OR (state<>'completed' AND canonical_result IS NULL))",
            name="ck_scheduled_job_result",
        ),
        sa.CheckConstraint(
            "((state IN ('retry','blocked','dead') AND error_code IS NOT NULL) "
            "OR (state NOT IN ('retry','blocked','dead') AND error_code IS NULL))",
            name="ck_scheduled_job_error",
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_request)>0 "
            "AND length(canonical_request)<=16384",
            name="ck_scheduled_job_input",
        ),
        sa.CheckConstraint(
            "canonical_result IS NULL OR length(canonical_result)<=16384",
            name="ck_scheduled_job_output",
        ),
        sa.CheckConstraint(
            "(attempts=0 AND last_run_id IS NULL) OR (attempts>0 AND last_run_id IS NOT NULL)",
            name="ck_scheduled_job_run",
        ),
    )
    op.create_index("ix_scheduled_jobs_due", "scheduled_jobs", ["kind", "state", "available_at"])
    op.create_table(
        "job_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("scheduled_jobs.id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("canonical_result", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.UniqueConstraint("job_id", "attempt", name="uq_scheduled_run_attempt"),
        sa.CheckConstraint(
            "attempt>=1 AND attempt<=10 AND lease_expires_at>started_at",
            name="ck_scheduled_run_bounds",
        ),
        sa.CheckConstraint(
            "state IN ('running','completed','retry','blocked','dead','expired')",
            name="ck_scheduled_run_state",
        ),
        sa.CheckConstraint(
            "((state='running' AND finished_at IS NULL) "
            "OR (state<>'running' AND finished_at IS NOT NULL AND finished_at>=started_at))",
            name="ck_scheduled_run_finished",
        ),
        sa.CheckConstraint(
            "((state='completed' AND canonical_result IS NOT NULL) "
            "OR (state<>'completed' AND canonical_result IS NULL))",
            name="ck_scheduled_run_result",
        ),
        sa.CheckConstraint(
            "canonical_result IS NULL OR length(canonical_result)<=16384",
            name="ck_scheduled_run_output",
        ),
        sa.CheckConstraint(
            "((state IN ('retry','blocked','dead','expired') AND error_code IS NOT NULL) "
            "OR (state NOT IN ('retry','blocked','dead','expired') AND error_code IS NULL))",
            name="ck_scheduled_run_error",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_weekly_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'weekly report history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER weekly_immutable BEFORE UPDATE OR DELETE ON usage_weekly_reports "
            "FOR EACH ROW EXECUTE FUNCTION aris_weekly_immutable()"
        )
        op.execute(
            "CREATE TRIGGER weekly_no_truncate BEFORE TRUNCATE ON usage_weekly_reports "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_weekly_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER weekly_no_{operation.lower()} BEFORE {operation} "
                "ON usage_weekly_reports BEGIN SELECT "
                "RAISE(ABORT,'weekly report history is immutable'); END"
            )
    for table, columns, terminal in (
        ("scheduled_jobs", JOB_DEFINITION, "OLD.state IN ('completed','blocked','dead')"),
        ("job_runs", RUN_DEFINITION, "OLD.state <> 'running'"),
    ):
        if dialect == "postgresql":
            changes = " OR ".join(f"NEW.{col} IS DISTINCT FROM OLD.{col}" for col in columns)
            op.execute(
                f"CREATE FUNCTION aris_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'scheduled history is immutable' "
                "USING ERRCODE='23000'; END IF; "
                f"IF ({changes}) OR ({terminal}) THEN "
                "RAISE EXCEPTION 'scheduled history is immutable' "
                "USING ERRCODE='23000'; END IF; RETURN NEW; END $$"
            )
            op.execute(
                f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION aris_{table}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION aris_{table}_guard()"
            )
        elif dialect == "sqlite":
            changes = " OR ".join(f"NEW.{col} IS NOT OLD.{col}" for col in columns)
            op.execute(
                f"CREATE TRIGGER {table}_update BEFORE UPDATE ON {table} "
                f"WHEN ({changes}) OR ({terminal}) BEGIN SELECT "
                "RAISE(ABORT,'scheduled history is immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT,'scheduled history is immutable'); END"
            )
        else:
            raise RuntimeError("unsupported scheduler database")


def downgrade() -> None:
    for table in ("job_runs", "scheduled_jobs", "usage_weekly_reports"):
        if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(table))):
            raise RuntimeError("scheduled history prevents downgrade")
    for table in ("job_runs", "scheduled_jobs"):
        op.drop_table(table)
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION aris_{table}_guard()")
    op.drop_table("usage_weekly_reports")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_weekly_immutable()")
