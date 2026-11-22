"""Save immutable weekly reports derived from daily GPU usage rollups."""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None
TABLE = "weekly_daily_coverage_results"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.String(36),
            sa.ForeignKey("weekly_coverage_schedules.id"),
            nullable=False,
        ),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_result", sa.Text(), nullable=False),
        sa.UniqueConstraint("schedule_id", "week", name="uq_weekly_daily_result_period"),
        sa.CheckConstraint(
            "length(fingerprint)=64 "
            "AND length(canonical_result)>0 AND length(canonical_result)<=262144",
            name="ck_weekly_daily_result_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_weekly_daily_result_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'weekly daily usage results are immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER weekly_daily_result_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_weekly_daily_result_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER weekly_daily_result_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_weekly_daily_result_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER weekly_daily_result_{operation.lower()} "
                f"BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT,'weekly daily usage results are immutable'); END"
            )
    else:
        raise RuntimeError("unsupported weekly report database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("weekly daily usage result history prevents downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_weekly_daily_result_immutable()")
