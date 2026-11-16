"""Actor- and intent-bound confirmations; preserve history on unsafe downgrade."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("commands") as batch:
        batch.add_column(sa.Column("confirmation_expires_at", sa.DateTime(timezone=True)))
        batch.alter_column("state", existing_type=sa.String(16), type_=sa.String(32))
        batch.drop_constraint("ck_command_state", type_="check")
        batch.create_check_constraint(
            "ck_command_state", "state IN ('pending','awaiting_confirmation','succeeded','expired')"
        )


def downgrade() -> None:
    table = sa.table("commands", sa.column("confirmation_expires_at", sa.DateTime(timezone=True)))
    if op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(table)
        .where(table.c.confirmation_expires_at.is_not(None))
    ):
        raise RuntimeError("confirmation history prevents downgrade")
    with op.batch_alter_table("commands") as batch:
        batch.drop_constraint("ck_command_state", type_="check")
        batch.create_check_constraint("ck_command_state", "state IN ('pending','succeeded')")
        batch.alter_column("state", existing_type=sa.String(32), type_=sa.String(16))
        batch.drop_column("confirmation_expires_at")
