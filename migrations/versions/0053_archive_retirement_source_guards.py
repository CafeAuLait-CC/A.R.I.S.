"""Serialize raw-reference capture with contiguous archive retirement."""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    invalid = connection.execute(
        sa.text(
            "SELECT r.agent_id FROM agent_message_receipts r LEFT JOIN nodes n "
            "ON n.agent_id=r.agent_id GROUP BY r.agent_id HAVING count(*)<>max(r.seq) "
            "OR count(n.id)<>count(*) LIMIT 1"
        )
    ).first()
    if invalid is not None:
        raise RuntimeError("Archive receipts require a verified contiguous source prefix")
    op.create_table(
        "agent_archive_states",
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("node_id", sa.String(128), sa.ForeignKey("nodes.id"), nullable=False),
        sa.Column("retired_through_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("retired_through_seq>=0", name="ck_archive_retirement_prefix"),
    )
    connection.execute(
        sa.text(
            "INSERT INTO agent_archive_states(agent_id,node_id,retired_through_seq) "
            "SELECT n.agent_id,n.id,coalesce(max(r.seq),0) FROM nodes n "
            "LEFT JOIN agent_message_receipts r ON r.agent_id=n.agent_id GROUP BY n.agent_id,n.id"
        )
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text("SELECT agent_id FROM agent_archive_states WHERE retired_through_seq>0 LIMIT 1")
        )
        .first()
        is not None
    ):
        raise RuntimeError("Cannot discard nonempty archive retirement source boundaries")
    op.drop_table("agent_archive_states")
