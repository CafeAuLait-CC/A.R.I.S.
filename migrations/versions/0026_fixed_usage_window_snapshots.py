"""Keep immutable metrics snapshots separate from source processing manifests."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_window_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(36), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_snapshot", sa.Text(), nullable=False),
        sa.CheckConstraint("end_at>start_at", name="ck_usage_window_range"),
        sa.CheckConstraint(
            "length(fingerprint)=64 AND length(canonical_snapshot)>0 "
            "AND length(canonical_snapshot)<=8388608",
            name="ck_usage_window_size",
        ),
    )
    op.create_index(
        "ix_usage_window_node_time", "usage_window_snapshots", ["node_id", "start_at", "end_at"]
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_windows_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'usage window history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER windows_immutable BEFORE UPDATE OR DELETE ON usage_window_snapshots "
            "FOR EACH ROW EXECUTE FUNCTION aris_windows_immutable()"
        )
        op.execute(
            "CREATE TRIGGER windows_no_truncate BEFORE TRUNCATE ON usage_window_snapshots "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_windows_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER windows_no_{operation.lower()} BEFORE {operation} "
                "ON usage_window_snapshots BEGIN SELECT "
                "RAISE(ABORT,'usage window history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported window snapshot database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_window_snapshots"))
    ):
        raise RuntimeError("usage window history prevents downgrade")
    op.drop_table("usage_window_snapshots")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_windows_immutable()")
