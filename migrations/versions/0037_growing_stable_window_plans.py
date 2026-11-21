"""Bounded rolling dispatch progress for open and finally closed residency layouts."""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_stable_window_plans",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("method_fingerprint", sa.String(64), nullable=False),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("canonical_origin", sa.Text(), nullable=False),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("next_window", sa.BigInteger(), nullable=False),
        sa.Column("record_revision", sa.BigInteger(), nullable=False),
        sa.Column("observed_intervals", sa.BigInteger(), nullable=False),
        sa.Column("planned_windows", sa.BigInteger(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_plan", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(key)=64 AND length(configuration_fingerprint)=64 "
            "AND length(method_fingerprint)=64 AND length(fingerprint)=64",
            name="ck_stable_plan_fingerprints",
        ),
        sa.CheckConstraint(
            "version>=1 AND next_window>=0 AND next_window<=planned_windows "
            "AND planned_windows>=0 AND planned_windows<=observed_intervals "
            "AND observed_intervals>=1 AND record_revision>=1",
            name="ck_stable_plan_counters",
        ),
        sa.CheckConstraint("updated_at>=created_at", name="ck_stable_plan_times"),
        sa.CheckConstraint(
            "length(canonical_origin)>0 AND length(canonical_origin)<=16384 "
            "AND length(canonical_plan)>0 AND length(canonical_plan)<=131072",
            name="ck_stable_plan_size",
        ),
    )
    immutable = (
        "key",
        "configuration_fingerprint",
        "method_fingerprint",
        "consumer_id",
        "node_id",
        "run_id",
        "canonical_origin",
        "event_id",
        "created_at",
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        fixed = " OR ".join(f"NEW.{name} IS DISTINCT FROM OLD.{name}" for name in immutable)
        op.execute(
            "CREATE FUNCTION aris_stable_plan_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF TG_OP='DELETE' OR TG_OP='TRUNCATE' THEN "
            "RAISE EXCEPTION 'stable plan history is retained' USING ERRCODE='23000'; END IF; "
            f"IF {fixed} OR NEW.version<>OLD.version+1 "
            "OR NEW.next_window<OLD.next_window OR NEW.record_revision<OLD.record_revision "
            "OR NEW.observed_intervals<OLD.observed_intervals "
            "OR NEW.planned_windows<OLD.planned_windows OR (OLD.closed AND NOT NEW.closed) "
            "OR NEW.updated_at<OLD.updated_at THEN "
            "RAISE EXCEPTION 'stable plan origin or progress differs' USING ERRCODE='23000'; "
            "END IF; RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER stable_plan_guard BEFORE UPDATE OR DELETE ON usage_stable_window_plans "
            "FOR EACH ROW EXECUTE FUNCTION aris_stable_plan_guard()"
        )
        op.execute(
            "CREATE TRIGGER stable_plan_no_truncate BEFORE TRUNCATE ON usage_stable_window_plans "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_stable_plan_guard()"
        )
    elif dialect == "sqlite":
        fixed = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in immutable)
        op.execute(
            "CREATE TRIGGER stable_plan_guard BEFORE UPDATE ON usage_stable_window_plans "
            f"WHEN {fixed} OR NEW.version<>OLD.version+1 "
            "OR NEW.next_window<OLD.next_window OR NEW.record_revision<OLD.record_revision "
            "OR NEW.observed_intervals<OLD.observed_intervals "
            "OR NEW.planned_windows<OLD.planned_windows OR (OLD.closed AND NOT NEW.closed) "
            "OR NEW.updated_at<OLD.updated_at BEGIN "
            "SELECT RAISE(ABORT,'stable plan origin or progress differs'); END"
        )
        op.execute(
            "CREATE TRIGGER stable_plan_no_delete BEFORE DELETE ON usage_stable_window_plans "
            "BEGIN SELECT RAISE(ABORT,'stable plan history is retained'); END"
        )
    else:
        raise RuntimeError("unsupported stable plan database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_stable_window_plans"))
    ):
        raise RuntimeError("stable plan history prevents downgrade")
    op.drop_table("usage_stable_window_plans")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_stable_plan_guard()")
