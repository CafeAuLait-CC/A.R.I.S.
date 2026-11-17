"""Durably fan out events and atomically deduplicate database consumers."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_publications",
        sa.Column(
            "event_id", sa.String(36), sa.ForeignKey("domain_events.event_id"), primary_key=True
        ),
        sa.Column("event_fingerprint", sa.String(64), nullable=False),
        sa.Column("registry_fingerprint", sa.String(64), nullable=False),
        sa.Column("subscriber_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("subscriber_count > 0", name="ck_publication_subscribers"),
    )
    op.create_table(
        "event_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id", sa.String(36), sa.ForeignKey("event_publications.event_id"), nullable=False
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("definition_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("replay_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("event_id", "consumer_id", name="uq_delivery_event_consumer"),
        sa.CheckConstraint(
            "state IN ('queued','running','processed','dead')", name="ck_delivery_state"
        ),
        sa.CheckConstraint("attempts >= 0 AND replay_count >= 0", name="ck_delivery_attempts"),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL) OR (state = 'queued' AND lease_token IS NULL AND "
            "lease_expires_at IS NULL AND completed_at IS NULL) OR (state IN "
            "('processed','dead') AND lease_token IS NULL AND lease_expires_at IS NULL AND "
            "completed_at IS NOT NULL)",
            name="ck_delivery_lease",
        ),
    )
    op.create_index(
        "ix_event_deliveries_ready",
        "event_deliveries",
        ["state", "available_at", "lease_expires_at"],
    )
    op.create_table(
        "event_consumer_inbox",
        sa.Column("consumer_id", sa.String(128), primary_key=True),
        sa.Column(
            "event_id", sa.String(36), sa.ForeignKey("domain_events.event_id"), primary_key=True
        ),
        sa.Column(
            "delivery_id", sa.String(36), sa.ForeignKey("event_deliveries.id"), nullable=False
        ),
        sa.Column("event_fingerprint", sa.String(64), nullable=False),
        sa.Column("definition_fingerprint", sa.String(64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("event_consumer_inbox", "event_deliveries", "event_publications"):
        if connection.scalar(sa.select(sa.func.count()).select_from(sa.table(name))):
            raise RuntimeError("event relay history prevents downgrade")
    op.drop_table("event_consumer_inbox")
    op.drop_index("ix_event_deliveries_ready", table_name="event_deliveries")
    op.drop_table("event_deliveries")
    op.drop_table("event_publications")
