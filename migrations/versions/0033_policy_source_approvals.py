"""Preserve individual source approvals, without activating policy rules."""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_source_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("policy_versions.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("requirements_fingerprint", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_approval", sa.Text(), nullable=False),
        sa.UniqueConstraint("command_id", name="uq_policy_source_approval_command"),
        sa.CheckConstraint(
            "source_version>0 AND expires_at>approved_at", name="ck_policy_source_approval_time"
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(requirements_fingerprint)=64 AND length(canonical_approval)>0 "
            "AND length(canonical_approval)<=32768",
            name="ck_policy_source_approval_size",
        ),
    )
    op.create_index(
        "ix_policy_source_approval_source_time",
        "policy_source_approvals",
        ["source_id", "approved_at", "id"],
    )
    op.create_table(
        "policy_source_approval_revocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "approval_id",
            sa.String(36),
            sa.ForeignKey("policy_source_approvals.id"),
            nullable=False,
        ),
        sa.Column("approval_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("revoked_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_revocation", sa.Text(), nullable=False),
        sa.UniqueConstraint("command_id", name="uq_policy_approval_revocation_command"),
        sa.UniqueConstraint("approval_id", name="uq_policy_approval_revocation_target"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(approval_fingerprint)=64 "
            "AND length(source_fingerprint)=64 AND length(canonical_revocation)>0 "
            "AND length(canonical_revocation)<=8192",
            name="ck_policy_approval_revocation_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_policy_source_approval_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'source approval is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        for table in ("policy_source_approvals", "policy_source_approval_revocations"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE "
                f"ON {table} FOR EACH ROW EXECUTE FUNCTION aris_policy_source_approval_immutable()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE "
                f"ON {table} FOR EACH STATEMENT "
                "EXECUTE FUNCTION aris_policy_source_approval_immutable()"
            )
    elif dialect == "sqlite":
        for table in ("policy_source_approvals", "policy_source_approval_revocations"):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT,'source approval is immutable'); END"
                )
    else:
        raise RuntimeError("unsupported source approval database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("policy_source_approval_revocations"))
    ):
        raise RuntimeError("source approval revocations prevent downgrade")
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("policy_source_approvals"))
    ):
        raise RuntimeError("source approval records prevent downgrade")
    op.drop_table("policy_source_approval_revocations")
    op.drop_table("policy_source_approvals")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_policy_source_approval_immutable()")
