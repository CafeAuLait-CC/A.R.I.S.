"""Serialize explicit identity initialization and administrative changes."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_directory_state",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("initial_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("subject_fingerprint", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("namespace", sa.String(128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 'primary'", name="ck_access_directory_singleton"),
        sa.CheckConstraint("revision >= 0", name="ck_access_directory_revision"),
        sa.CheckConstraint(
            "(completed_at IS NULL AND initial_user_id IS NULL AND "
            "subject_fingerprint IS NULL AND provider IS NULL AND namespace IS NULL "
            "AND revision = 0) OR "
            "(completed_at IS NOT NULL AND initial_user_id IS NOT NULL AND "
            "subject_fingerprint IS NOT NULL AND provider IS NOT NULL "
            "AND namespace IS NOT NULL AND revision > 0)",
            name="ck_access_directory_initialization",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM access_directory_state WHERE completed_at IS NOT NULL")
    ):
        raise RuntimeError("identity administration history prevents downgrade")
    op.drop_table("access_directory_state")
