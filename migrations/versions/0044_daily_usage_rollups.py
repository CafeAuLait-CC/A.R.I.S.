"""Store consolidated day evidence separately from sample and interval storage."""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None
TABLE = "usage_daily_rollups"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_rollup", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(snapshot_fingerprint)=64 "
            "AND length(canonical_rollup)>0 AND length(canonical_rollup)<=262144",
            name="ck_daily_usage_rollup_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_daily_rollup_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'daily usage rollups are immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER daily_rollup_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_daily_rollup_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER daily_rollup_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_daily_rollup_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER daily_rollup_{operation.lower()} BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT,'daily usage rollups are immutable'); END"
            )
    else:
        raise RuntimeError("unsupported daily rollup database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("daily usage rollup history prevents downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_daily_rollup_immutable()")
