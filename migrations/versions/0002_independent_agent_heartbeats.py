"""Separate Agent liveness from GPU facts; share message identity across report types."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("telemetry_at", "telemetry_received_at", "heartbeat_at", "heartbeat_received_at"):
        op.add_column("nodes", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in ("telemetry_seq", "heartbeat_seq"):
        op.add_column("nodes", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column(
        "nodes",
        sa.Column("collector_status", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.execute(
        "UPDATE nodes SET telemetry_at=observed_at, "
        "telemetry_received_at=received_at, telemetry_seq=last_seq"
    )
    op.rename_table("telemetry_messages", "agent_messages")
    op.drop_index("ix_telemetry_messages_observed_at", table_name="agent_messages")
    op.create_index("ix_agent_messages_observed_at", "agent_messages", ["observed_at"])
    op.add_column(
        "agent_messages",
        sa.Column("message_kind", sa.String(16), nullable=False, server_default="telemetry"),
    )


def downgrade() -> None:
    # A downgrade cannot represent heartbeat-only nodes or their accepted messages.
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM agent_messages WHERE message_kind='heartbeat'")
    )
    if count:
        raise RuntimeError(
            "heartbeat data exists; export and explicitly resolve it before downgrade"
        )
    op.drop_column("agent_messages", "message_kind")
    op.drop_index("ix_agent_messages_observed_at", table_name="agent_messages")
    op.create_index("ix_telemetry_messages_observed_at", "agent_messages", ["observed_at"])
    op.rename_table("agent_messages", "telemetry_messages")
    for name in (
        "telemetry_at",
        "telemetry_received_at",
        "telemetry_seq",
        "heartbeat_at",
        "heartbeat_received_at",
        "heartbeat_seq",
        "collector_status",
    ):
        op.drop_column("nodes", name)
