"""Keep immutable supplied-case review material separate from policy approval."""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_review_materials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("policy_versions.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("implementation_ref", sa.String(128), nullable=False),
        sa.Column("scenarios", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_material", sa.Text(), nullable=False),
        sa.UniqueConstraint("command_id", name="uq_policy_material_command"),
        sa.CheckConstraint(
            "source_version>0 AND scenarios>=1 AND scenarios<=8", name="ck_policy_material_scope"
        ),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(canonical_material)>0 AND length(canonical_material)<=65536",
            name="ck_policy_material_size",
        ),
    )
    op.create_index(
        "ix_policy_material_source_time",
        "policy_review_materials",
        ["source_id", "created_at", "id"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_policy_material_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'policy review material is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER policy_material_immutable BEFORE UPDATE OR DELETE "
            "ON policy_review_materials FOR EACH ROW "
            "EXECUTE FUNCTION aris_policy_material_immutable()"
        )
        op.execute(
            "CREATE TRIGGER policy_material_no_truncate BEFORE TRUNCATE "
            "ON policy_review_materials FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_policy_material_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER policy_material_no_{operation.lower()} BEFORE {operation} "
                "ON policy_review_materials BEGIN SELECT "
                "RAISE(ABORT,'policy review material is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported policy material database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("policy_review_materials"))
    ):
        raise RuntimeError("policy review material history prevents downgrade")
    op.drop_table("policy_review_materials")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_policy_material_immutable()")
