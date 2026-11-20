"""Append source-bound human feedback without approving or publishing a policy."""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_review_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("policy_versions.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("policy_review_materials.id")),
        sa.Column("material_fingerprint", sa.String(64)),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_feedback", sa.Text(), nullable=False),
        sa.UniqueConstraint("command_id", name="uq_policy_feedback_command"),
        sa.CheckConstraint(
            "source_version>0 AND kind IN ('comment','changes_requested')",
            name="ck_policy_feedback_kind",
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(canonical_feedback)>0 AND length(canonical_feedback)<=16384",
            name="ck_policy_feedback_size",
        ),
        sa.CheckConstraint(
            "(material_id IS NULL AND material_fingerprint IS NULL) OR "
            "(material_id IS NOT NULL AND material_fingerprint IS NOT NULL "
            "AND length(material_fingerprint)=64)",
            name="ck_policy_feedback_material",
        ),
    )
    op.create_index(
        "ix_policy_feedback_source_time",
        "policy_review_feedback",
        ["source_id", "created_at", "id"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_policy_feedback_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'policy feedback is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER policy_feedback_immutable BEFORE UPDATE OR DELETE "
            "ON policy_review_feedback FOR EACH ROW "
            "EXECUTE FUNCTION aris_policy_feedback_immutable()"
        )
        op.execute(
            "CREATE TRIGGER policy_feedback_no_truncate BEFORE TRUNCATE "
            "ON policy_review_feedback FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_policy_feedback_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER policy_feedback_no_{operation.lower()} BEFORE {operation} "
                "ON policy_review_feedback BEGIN SELECT "
                "RAISE(ABORT,'policy feedback is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported policy feedback database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("policy_review_feedback"))
    ):
        raise RuntimeError("policy feedback history prevents downgrade")
    op.drop_table("policy_review_feedback")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_policy_feedback_immutable()")
