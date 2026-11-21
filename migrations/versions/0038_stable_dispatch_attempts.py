"""Durable preparation backoff and fair pending stable-layout selection."""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_stable_plan_pending",
        "usage_stable_window_plans",
        ["configuration_fingerprint", "method_fingerprint", "created_at", "key"],
        postgresql_where=sa.text("next_window<planned_windows"),
        sqlite_where=sa.text("next_window<planned_windows"),
    )
    op.create_table(
        "usage_stable_dispatch_states",
        sa.Column(
            "key", sa.String(64), sa.ForeignKey("usage_stable_window_plans.key"), primary_key=True
        ),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("observed_plan_version", sa.BigInteger(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.CheckConstraint(
            "version>=1 AND observed_plan_version>=1 AND failures>=0 AND failures<=20",
            name="ck_stable_dispatch_counters",
        ),
        sa.CheckConstraint(
            "available_at>=updated_at AND updated_at>=created_at", name="ck_stable_dispatch_times"
        ),
        sa.CheckConstraint(
            "(failures=0 AND last_error_code IS NULL AND NOT blocked) "
            "OR (failures>0 AND last_error_code IS NOT NULL)",
            name="ck_stable_dispatch_outcome",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_stable_dispatch_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF TG_OP='DELETE' OR TG_OP='TRUNCATE' THEN "
            "RAISE EXCEPTION 'stable dispatch history is retained' USING ERRCODE='23000'; END IF; "
            "IF TG_OP='UPDATE' AND (NEW.key IS DISTINCT FROM OLD.key "
            "OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.version<>OLD.version+1 "
            "OR NEW.observed_plan_version<OLD.observed_plan_version "
            "OR NEW.updated_at<OLD.updated_at "
            "OR NEW.event_id IS NOT DISTINCT FROM OLD.event_id) THEN "
            "RAISE EXCEPTION 'stable dispatch progress differs' USING ERRCODE='23000'; END IF; "
            "IF NEW.observed_plan_version<>"
            "(SELECT version FROM usage_stable_window_plans WHERE key=NEW.key) THEN "
            "RAISE EXCEPTION 'stable dispatch source version differs' "
            "USING ERRCODE='23000'; END IF; "
            "RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER stable_dispatch_guard BEFORE INSERT OR UPDATE OR DELETE "
            "ON usage_stable_dispatch_states FOR EACH ROW "
            "EXECUTE FUNCTION aris_stable_dispatch_guard()"
        )
        op.execute(
            "CREATE TRIGGER stable_dispatch_no_truncate BEFORE TRUNCATE "
            "ON usage_stable_dispatch_states "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_stable_dispatch_guard()"
        )
    elif dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER stable_dispatch_update BEFORE UPDATE ON usage_stable_dispatch_states "
            "WHEN NEW.key IS NOT OLD.key OR NEW.created_at IS NOT OLD.created_at "
            "OR NEW.version<>OLD.version+1 OR NEW.observed_plan_version<OLD.observed_plan_version "
            "OR NEW.updated_at<OLD.updated_at OR NEW.event_id IS OLD.event_id "
            "BEGIN SELECT RAISE(ABORT,'stable dispatch progress differs'); END"
        )
        for operation in ("INSERT", "UPDATE"):
            op.execute(
                f"CREATE TRIGGER stable_dispatch_{operation.lower()}_source BEFORE {operation} "
                "ON usage_stable_dispatch_states WHEN NEW.observed_plan_version<> "
                "(SELECT version FROM usage_stable_window_plans WHERE key=NEW.key) "
                "BEGIN SELECT RAISE(ABORT,'stable dispatch source version differs'); END"
            )
        op.execute(
            "CREATE TRIGGER stable_dispatch_no_delete BEFORE DELETE "
            "ON usage_stable_dispatch_states "
            "BEGIN SELECT RAISE(ABORT,'stable dispatch history is retained'); END"
        )
    else:
        raise RuntimeError("unsupported stable dispatch database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_stable_dispatch_states"))
    ):
        raise RuntimeError("stable dispatch history prevents downgrade")
    op.drop_table("usage_stable_dispatch_states")
    op.drop_index("ix_stable_plan_pending", table_name="usage_stable_window_plans")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_stable_dispatch_guard()")
