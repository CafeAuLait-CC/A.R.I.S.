"""Current component observations and shared GPU collector health ordering."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("collector_health_seq", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "nodes", sa.Column("collector_health_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE nodes SET collector_health_seq=heartbeat_seq, collector_health_at=heartbeat_at"
    )
    op.create_table(
        "node_resources",
        sa.Column("node_id", sa.String(128), sa.ForeignKey("nodes.id"), primary_key=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("node_resources")
    op.drop_column("nodes", "collector_health_at")
    op.drop_column("nodes", "collector_health_seq")
