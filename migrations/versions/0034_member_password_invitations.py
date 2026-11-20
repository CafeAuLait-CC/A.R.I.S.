"""Member usernames, salted password hashes, private invitations and contact email."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.create_table(
        "user_password_accounts",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=True),
        sa.Column("challenge_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("challenge_version >= 0", name="ck_password_challenge_version"),
    )
    op.create_table(
        "user_login_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "identity_id", sa.String(36), sa.ForeignKey("external_identities.id"), nullable=False
        ),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose IN ('activate','reset')", name="ck_login_invitation_purpose"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_login_invitation_attempts"),
        sa.CheckConstraint("length(code_digest) = 64", name="ck_login_invitation_digest"),
        sa.CheckConstraint("expires_at > created_at", name="ck_login_invitation_expiration"),
    )
    op.create_index(
        "ix_login_invitations_user_created", "user_login_invitations", ["user_id", "created_at"]
    )
    op.create_table(
        "login_rate_windows",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_login_rate_attempts"),
    )


def downgrade() -> None:
    for table in ("user_password_accounts", "user_login_invitations", "login_rate_windows"):
        if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(table))):
            raise RuntimeError("Account authentication history exists; use a forward repair")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM users WHERE email IS NOT NULL")):
        raise RuntimeError("Contact emails exist; use a forward repair")
    op.drop_table("login_rate_windows")
    op.drop_table("user_login_invitations")
    op.drop_table("user_password_accounts")
    op.drop_column("users", "email")
