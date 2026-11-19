"""Keep the next unprepared report week across process restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

DEFINITION = (
    "id",
    "plan_key",
    "node_id",
    "owner_id",
    "report_type",
    "first_week",
    "canonical_plan",
    "fingerprint",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "weekly_report_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_key", sa.String(512), nullable=False, unique=True),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=False),
        sa.Column("report_type", sa.String(64), nullable=False),
        sa.Column("first_week", sa.Date(), nullable=False),
        sa.Column("canonical_plan", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("next_week", sa.Date(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_prepared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "next_week>=first_week AND updated_at>=created_at AND revision>=1",
            name="ck_weekly_plan_progress",
        ),
        sa.CheckConstraint(
            "consecutive_failures>=0 AND consecutive_failures<=10", name="ck_weekly_plan_failures"
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_weekly_plan_lease",
        ),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND retry_at IS NULL AND consecutive_failures=0) "
            "OR (last_error_code IS NOT NULL AND retry_at IS NOT NULL AND consecutive_failures>0)",
            name="ck_weekly_plan_error",
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_plan)>0 AND length(canonical_plan)<=16384",
            name="ck_weekly_plan_size",
        ),
    )
    op.create_index(
        "ix_weekly_plan_scope", "weekly_report_plans", ["node_id", "owner_id", "report_type"]
    )
    dialect = op.get_bind().dialect.name
    progress = (
        "NEW.next_week<OLD.next_week OR NEW.updated_at<OLD.updated_at OR NEW.revision<OLD.revision"
    )
    if dialect == "postgresql":
        changes = " OR ".join(f"NEW.{col} IS DISTINCT FROM OLD.{col}" for col in DEFINITION)
        op.execute(
            "CREATE FUNCTION aris_weekly_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'weekly plans retain history' "
            "USING ERRCODE='23000'; END IF; "
            f"IF ({changes}) OR ({progress}) THEN "
            "RAISE EXCEPTION 'weekly plan definition is immutable' "
            "USING ERRCODE='23000'; END IF; RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER weekly_plan_guard BEFORE UPDATE OR DELETE ON weekly_report_plans "
            "FOR EACH ROW EXECUTE FUNCTION aris_weekly_plan_guard()"
        )
        op.execute(
            "CREATE TRIGGER weekly_plan_truncate BEFORE TRUNCATE ON weekly_report_plans "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_weekly_plan_guard()"
        )
    elif dialect == "sqlite":
        changes = " OR ".join(f"NEW.{col} IS NOT OLD.{col}" for col in DEFINITION)
        op.execute(
            "CREATE TRIGGER weekly_plan_update BEFORE UPDATE ON weekly_report_plans "
            f"WHEN ({changes}) OR ({progress}) BEGIN SELECT "
            "RAISE(ABORT,'weekly plan definition is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER weekly_plan_delete BEFORE DELETE ON weekly_report_plans "
            "BEGIN SELECT RAISE(ABORT,'weekly plans retain history'); END"
        )
    else:
        raise RuntimeError("unsupported weekly plan database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("weekly_report_plans"))
    ):
        raise RuntimeError("weekly plan history prevents downgrade")
    op.drop_table("weekly_report_plans")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_weekly_plan_guard()")
