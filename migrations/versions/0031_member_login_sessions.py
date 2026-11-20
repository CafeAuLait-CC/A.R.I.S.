"""Explicit member portal login credentials and server-side sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_login_credentials",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_login_credential_digest"),
        sa.CheckConstraint("generation >= 1", name="ck_login_credential_generation"),
    )
    op.create_table(
        "user_login_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "identity_id", sa.String(36), sa.ForeignKey("external_identities.id"), nullable=False
        ),
        sa.Column("credential_generation", sa.Integer(), nullable=True),
        sa.Column("credential_digest", sa.String(64), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_login_session_digest"),
        sa.CheckConstraint("length(csrf_digest) = 64", name="ck_login_session_csrf"),
        sa.CheckConstraint("length(credential_digest) = 64", name="ck_login_session_credential"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_login_session_expiration"),
    )
    op.create_index("ix_login_sessions_user", "user_login_sessions", ["user_id", "expires_at"])


def downgrade() -> None:
    for table in ("user_login_sessions", "user_login_credentials"):
        if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(table))):
            raise RuntimeError("Login history exists; use a forward repair")
    op.drop_table("user_login_sessions")
    op.drop_table("user_login_credentials")
