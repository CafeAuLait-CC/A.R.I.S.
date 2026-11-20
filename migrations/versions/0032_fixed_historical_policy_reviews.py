"""Preserve exact historical review contexts without approval or publication."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_historical_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("policy_versions.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("implementation_ref", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.UniqueConstraint("command_id", name="uq_policy_historical_review_command"),
        sa.CheckConstraint("source_version>0", name="ck_policy_historical_review_version"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(source_fingerprint)=64 "
            "AND length(context_fingerprint)=64 AND length(canonical_record)>0 "
            "AND length(canonical_record)<=2097152",
            name="ck_policy_historical_review_size",
        ),
    )
    op.create_index(
        "ix_policy_history_source_owner_node_time",
        "policy_historical_reviews",
        ["source_id", "created_by", "node_id", "created_at", "id"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_policy_history_review_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'historical review is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER policy_history_review_immutable BEFORE UPDATE OR DELETE "
            "ON policy_historical_reviews FOR EACH ROW "
            "EXECUTE FUNCTION aris_policy_history_review_immutable()"
        )
        op.execute(
            "CREATE TRIGGER policy_history_review_no_truncate BEFORE TRUNCATE "
            "ON policy_historical_reviews FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_policy_history_review_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER policy_history_review_no_{operation.lower()} BEFORE {operation} "
                "ON policy_historical_reviews BEGIN SELECT "
                "RAISE(ABORT,'historical review is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported historical review database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("policy_historical_reviews"))
    ):
        raise RuntimeError("historical review records prevent downgrade")
    op.drop_table("policy_historical_reviews")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_policy_history_review_immutable()")
