"""Persist explicit weekly coverage progress only after a linked snapshot is saved."""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None
DEFINITION = ("id", "canonical_definition", "fingerprint", "first_week", "created_at")


def upgrade() -> None:
    op.create_table(
        "weekly_coverage_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("canonical_definition", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("first_week", sa.Date(), nullable=False),
        sa.Column("next_week", sa.Date(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_result_id", sa.String(36)),
        sa.CheckConstraint(
            "revision>=1 AND next_week>=first_week AND updated_at>=created_at",
            name="ck_coverage_schedule_progress",
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_definition)>0 "
            "AND length(canonical_definition)<=16384",
            name="ck_coverage_schedule_definition",
        ),
    )
    op.create_table(
        "weekly_coverage_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.String(36),
            sa.ForeignKey("weekly_coverage_schedules.id"),
            nullable=False,
        ),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column(
            "snapshot_id", sa.String(36), sa.ForeignKey("usage_window_snapshots.id"), nullable=False
        ),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_result", sa.Text(), nullable=False),
        sa.UniqueConstraint("schedule_id", "week", name="uq_coverage_result_week"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_result)>0 "
            "AND length(canonical_result)<=16384",
            name="ck_coverage_result_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    progress = (
        "NEW.next_week<OLD.next_week OR NEW.updated_at<OLD.updated_at OR NEW.revision<=OLD.revision"
    )
    if dialect == "postgresql":
        changes = " OR ".join(f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in DEFINITION)
        op.execute(
            "CREATE FUNCTION aris_coverage_schedule_guard() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN IF TG_OP<>'UPDATE' THEN RAISE EXCEPTION "
            "'coverage schedules retain history' "
            "USING ERRCODE='23000'; END IF; " + f"IF ({changes}) OR ({progress}) THEN "
            "RAISE EXCEPTION 'coverage schedule definition is immutable' "
            "USING ERRCODE='23000'; END IF; RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER coverage_schedule_guard BEFORE UPDATE OR DELETE "
            "ON weekly_coverage_schedules "
            "FOR EACH ROW EXECUTE FUNCTION aris_coverage_schedule_guard()"
        )
        op.execute(
            "CREATE TRIGGER coverage_schedule_truncate BEFORE TRUNCATE "
            "ON weekly_coverage_schedules "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_coverage_schedule_guard()"
        )
        op.execute(
            "CREATE FUNCTION aris_coverage_result_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'coverage results are immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER coverage_result_guard BEFORE UPDATE OR DELETE "
            "ON weekly_coverage_results "
            "FOR EACH ROW EXECUTE FUNCTION aris_coverage_result_guard()"
        )
        op.execute(
            "CREATE TRIGGER coverage_result_truncate BEFORE TRUNCATE ON weekly_coverage_results "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_coverage_result_guard()"
        )
    elif dialect == "sqlite":
        changes = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in DEFINITION)
        op.execute(
            "CREATE TRIGGER coverage_schedule_update BEFORE UPDATE ON weekly_coverage_schedules "
            + f"WHEN ({changes}) OR ({progress}) BEGIN SELECT "
            "RAISE(ABORT,'coverage schedule definition is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER coverage_schedule_delete BEFORE DELETE ON weekly_coverage_schedules "
            "BEGIN SELECT RAISE(ABORT,'coverage schedules retain history'); END"
        )
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER coverage_result_{action.lower()} BEFORE {action} "
                "ON weekly_coverage_results "
                "BEGIN SELECT RAISE(ABORT,'coverage results are immutable'); END"
            )
    else:
        raise RuntimeError("unsupported coverage schedule database")


def downgrade() -> None:
    if any(
        op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(name)))
        for name in ("weekly_coverage_results", "weekly_coverage_schedules")
    ):
        raise RuntimeError("coverage schedule history prevents downgrade")
    op.drop_table("weekly_coverage_results")
    op.drop_table("weekly_coverage_schedules")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_coverage_result_guard()")
        op.execute("DROP FUNCTION aris_coverage_schedule_guard()")
