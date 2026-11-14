"""Minimal Slack event receipts and leased Home publishing jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slack_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(192), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("team_id", "external_id", name="uq_slack_receipt"),
    )
    op.create_table(
        "slack_home_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("slack_receipts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("team_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
    )
    op.create_index("ix_slack_home_jobs_state", "slack_home_jobs", ["state"])
    op.create_index("ix_slack_home_jobs_available_at", "slack_home_jobs", ["available_at"])


def downgrade() -> None:
    op.drop_index("ix_slack_home_jobs_available_at", table_name="slack_home_jobs")
    op.drop_index("ix_slack_home_jobs_state", table_name="slack_home_jobs")
    op.drop_table("slack_home_jobs")
    op.drop_table("slack_receipts")
