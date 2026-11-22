"""Keep one resumable reporting scan position per authenticated source."""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None
TABLE = "reporting_gap_scan_progress"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        sa.Column("canonical_position", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "through_seq > 0 AND length(canonical_position) BETWEEN 1 AND 4096",
            name="ck_reporting_gap_scan_progress_value",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(TABLE))):
        raise RuntimeError("reporting gap scan progress prevents downgrade")
    op.drop_table(TABLE)
