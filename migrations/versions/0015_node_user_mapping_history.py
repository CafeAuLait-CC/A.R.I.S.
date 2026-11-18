"""Preserve future mapping changes without inventing legacy effective dates."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_user_mapping_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("os_username", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_id", sa.String(36), sa.ForeignKey("domain_events.event_id"), nullable=False
        ),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("before_user_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("before_confidence", sa.String(16)),
        sa.Column("before_source", sa.String(64)),
        sa.Column("after_user_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("after_confidence", sa.String(16)),
        sa.Column("after_source", sa.String(64)),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "node_id", "os_username", "revision", name="uq_mapping_history_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_mapping_history_revision"),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_mapping_history_fingerprint"),
        sa.CheckConstraint(
            "(before_user_id IS NULL AND before_confidence IS NULL AND before_source IS NULL) "
            "OR (before_user_id IS NOT NULL AND before_confidence IN ('verified','unverified') "
            "AND before_source IS NOT NULL)",
            name="ck_mapping_history_before",
        ),
        sa.CheckConstraint(
            "(after_user_id IS NULL AND after_confidence IS NULL AND after_source IS NULL) "
            "OR (after_user_id IS NOT NULL AND after_confidence IN ('verified','unverified') "
            "AND after_source IS NOT NULL)",
            name="ck_mapping_history_after",
        ),
    )
    op.create_index(
        "ix_mapping_history_time",
        "node_user_mapping_history",
        ["node_id", "os_username", "effective_at", "revision"],
    )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_mapping_history_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'mapping history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER mapping_history_immutable BEFORE UPDATE OR DELETE "
            "ON node_user_mapping_history "
            "FOR EACH ROW EXECUTE FUNCTION aris_mapping_history_immutable()"
        )
        op.execute(
            "CREATE TRIGGER mapping_history_no_truncate BEFORE TRUNCATE "
            "ON node_user_mapping_history "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_mapping_history_immutable()"
        )
    elif connection.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                "CREATE TRIGGER mapping_history_no_"
                + operation.lower()
                + " BEFORE "
                + operation
                + " ON node_user_mapping_history BEGIN "
                "SELECT RAISE(ABORT,'mapping history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported mapping history database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.select(sa.func.count()).select_from(sa.table("node_user_mapping_history"))
    ):
        raise RuntimeError("mapping history prevents downgrade")
    op.drop_index("ix_mapping_history_time", table_name="node_user_mapping_history")
    op.drop_table("node_user_mapping_history")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_mapping_history_immutable()")
