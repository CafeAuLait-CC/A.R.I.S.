"""Keep consecutively observed reporting gaps beyond the raw archive lifetime."""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None
TABLE = "reporting_gap_history"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("next_seq", sa.BigInteger(), primary_key=True),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "next_seq > 1 AND length(canonical_record) BETWEEN 1 AND 4096",
            name="ck_reporting_gap_history_value",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_reporting_gap_history_immutable() "
            "RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'reporting gap history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER reporting_gap_history_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_reporting_gap_history_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER reporting_gap_history_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_reporting_gap_history_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER reporting_gap_history_{operation.lower()} "
                f"BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT,'reporting gap history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported reporting history database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("saved reporting gap history prevents downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_reporting_gap_history_immutable()")
