"""Track a finite historical health scan without resetting the live scan."""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None
TABLE = "reporting_history_prefix_progress"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("target_through_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("canonical_position", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "last_seq > 0 AND target_through_seq >= last_seq "
            "AND length(canonical_position) BETWEEN 1 AND 4096",
            name="ck_reporting_history_prefix_value",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("historical reporting prefix progress prevents downgrade")
    op.drop_table(TABLE)
