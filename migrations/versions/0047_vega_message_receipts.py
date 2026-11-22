"""Keep exact duplicate acknowledgments without keeping message payloads."""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None
TABLE = "agent_message_receipts"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("applied_to_current", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("agent_id", "message_id", name="uq_agent_message_receipt_id"),
        sa.CheckConstraint(
            "seq > 0 AND length(fingerprint)=64",
            name="ck_agent_message_receipt_identity",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_agent_message_receipt_immutable() "
            "RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'agent message receipts are immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER agent_message_receipt_immutable BEFORE UPDATE OR DELETE ON {TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_agent_message_receipt_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER agent_message_receipt_no_truncate BEFORE TRUNCATE ON {TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_agent_message_receipt_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER agent_message_receipt_{operation.lower()} "
                f"BEFORE {operation} ON {TABLE} "
                "BEGIN SELECT RAISE(ABORT,'agent message receipts are immutable'); END"
            )
    else:
        raise RuntimeError("unsupported receipt database")


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("agent message receipt history prevents downgrade")
    op.drop_table(TABLE)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_agent_message_receipt_immutable()")
