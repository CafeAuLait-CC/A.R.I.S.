"""Record confirmed immutable policy sources without approving or publishing them."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "source_version", name="uq_policy_source_version"),
        sa.CheckConstraint("source_version > 0", name="ck_policy_source_version"),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_policy_source_fingerprint"),
        sa.CheckConstraint(
            "length(canonical_json) > 0 AND length(canonical_json) <= 65536",
            name="ck_policy_source_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_policy_source_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'policy source history is immutable' USING ERRCODE = '23000'; "
            "END $$"
        )
        op.execute(
            "CREATE TRIGGER policy_source_immutable BEFORE UPDATE OR DELETE ON policy_versions "
            "FOR EACH ROW EXECUTE FUNCTION aris_policy_source_immutable()"
        )
        op.execute(
            "CREATE TRIGGER policy_source_no_truncate BEFORE TRUNCATE ON policy_versions "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_policy_source_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                "CREATE TRIGGER policy_source_no_"
                + operation.lower()
                + " BEFORE "
                + operation
                + " ON policy_versions BEGIN "
                "SELECT RAISE(ABORT, 'policy source history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported policy source database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(sa.table("policy_versions"))):
        raise RuntimeError("policy source history prevents downgrade")
    op.drop_table("policy_versions")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_policy_source_immutable()")
