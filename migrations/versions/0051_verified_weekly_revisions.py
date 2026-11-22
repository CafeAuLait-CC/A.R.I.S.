"""Keep independently verified draft week versions tied to their original report."""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None
TABLE = "weekly_daily_verified_revisions"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "original_report_id",
            sa.String(36),
            sa.ForeignKey("weekly_daily_coverage_results.id"),
            primary_key=True,
        ),
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_revision", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_revision) BETWEEN 1 AND 262144",
            name="ck_weekly_daily_verified_revision",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_verified_weekly_revision_immutable() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'verified weekly revision is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER verified_weekly_revision_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_verified_weekly_revision_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER verified_weekly_revision_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_verified_weekly_revision_immutable()"
        )
    else:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER verified_weekly_revision_{operation.lower()} "
                f"BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT, 'verified weekly revision is immutable'); END"
            )


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("verified weekly revisions prevent downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_verified_weekly_revision_immutable()")
