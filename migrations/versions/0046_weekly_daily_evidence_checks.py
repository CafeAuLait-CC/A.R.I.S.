"""Retain successful evidence checks for an immutable weekly report and day."""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None
TABLE = "weekly_daily_evidence_checks"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "report_id",
            sa.String(36),
            sa.ForeignKey("weekly_daily_coverage_results.id"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("report_fingerprint", sa.String(64), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(64), nullable=False),
        sa.Column("rollup_fingerprint", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(report_fingerprint)=64 AND length(snapshot_fingerprint)=64 "
            "AND length(rollup_fingerprint)=64",
            name="ck_weekly_evidence_check_fingerprints",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_weekly_evidence_check_immutable() "
            "RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'daily usage evidence checks are immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER weekly_evidence_check_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_weekly_evidence_check_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER weekly_evidence_check_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_weekly_evidence_check_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER weekly_evidence_check_{operation.lower()} "
                f"BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT,'daily usage evidence checks are immutable'); END"
            )
    else:
        raise RuntimeError("unsupported evidence check database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("daily usage evidence check history prevents downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_weekly_evidence_check_immutable()")
