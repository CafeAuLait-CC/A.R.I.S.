"""Persist notification snapshots, send phases and external delivery evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id", sa.String(36), sa.ForeignKey("domain_events.event_id"), nullable=False
        ),
        sa.Column("recipient_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("template_fingerprint", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("dedupe_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_id", sa.String(36)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dedupe_sha256", name="uq_notification_dedupe"),
        sa.CheckConstraint(
            "state IN ('queued','running','delivered','failed','uncertain')",
            name="ck_notification_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_attempts"),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND last_attempt_id IS NOT NULL AND last_attempt_id = lease_token AND "
            "completed_at IS NULL) OR (state = 'queued' "
            "AND lease_token IS NULL AND lease_expires_at IS NULL AND completed_at IS NULL) OR "
            "(state IN ('delivered','failed','uncertain') AND lease_token IS NULL AND "
            "lease_expires_at IS NULL AND completed_at IS NOT NULL)",
            name="ck_notification_lease",
        ),
        sa.CheckConstraint(
            "state NOT IN ('delivered','uncertain') OR "
            "(last_attempt_id IS NOT NULL AND attempts > 0)",
            name="ck_notification_evidence",
        ),
    )
    op.create_index(
        "ix_notifications_ready",
        "notification_outbox",
        ["state", "available_at", "lease_expires_at"],
    )
    op.create_table(
        "notification_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "notification_id",
            sa.String(36),
            sa.ForeignKey("notification_outbox.id"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("channel_ref", sa.String(128)),
        sa.Column("message_ref", sa.String(128)),
        sa.UniqueConstraint("notification_id", "attempt", name="uq_notification_attempt"),
        sa.CheckConstraint("attempt > 0", name="ck_notification_attempt_number"),
        sa.CheckConstraint(
            "state IN ('claimed','sending','delivered','rejected','uncertain',"
            "'abandoned','late_delivered')",
            name="ck_notification_attempt_state",
        ),
        sa.CheckConstraint(
            "(state IN ('claimed','sending') AND finished_at IS NULL) OR "
            "(state IN ('delivered','rejected','uncertain','abandoned','late_delivered') AND "
            "finished_at IS NOT NULL)",
            name="ck_notification_attempt_finished",
        ),
        sa.CheckConstraint(
            "(state IN ('delivered','late_delivered') AND channel_ref IS NOT NULL AND "
            "message_ref IS NOT NULL) OR (state NOT IN ('delivered','late_delivered') AND "
            "channel_ref IS NULL AND message_ref IS NULL)",
            name="ck_notification_attempt_receipt",
        ),
        sa.CheckConstraint(
            "(state IN ('claimed','abandoned') AND prepared_at IS NULL) OR "
            "(state IN ('sending','uncertain','delivered','late_delivered') AND "
            "prepared_at IS NOT NULL) OR state = 'rejected'",
            name="ck_notification_attempt_phase",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("notification_attempts", "notification_outbox"):
        if connection.scalar(sa.select(sa.func.count()).select_from(sa.table(name))):
            raise RuntimeError("notification delivery history prevents downgrade")
    op.drop_table("notification_attempts")
    op.drop_index("ix_notifications_ready", table_name="notification_outbox")
    op.drop_table("notification_outbox")
