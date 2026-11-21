"""Bounded source exclusion checkpoints and absolute build progress counters."""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None
_TABLE = "usage_source_prefix_checkpoints"


def _progress_check(bounded: bool) -> None:
    connection = op.get_bind()
    triggers = []
    if connection.dialect.name == "sqlite":
        triggers = list(
            connection.scalars(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='usage_coverage_build_steps'"
                )
            )
        )
    with op.batch_alter_table("usage_coverage_build_steps") as batch:
        batch.drop_constraint("coverage_build_step_revision", type_="check")
        batch.create_check_constraint(
            "coverage_build_step_revision",
            "revision >= 1 AND revision <= 4096" if bounded else "revision >= 1",
        )
    for trigger in triggers:
        op.execute(trigger)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("previous_id", sa.String(36), sa.ForeignKey(_TABLE + ".id"), nullable=True),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("through_revision", sa.BigInteger(), nullable=False),
        sa.Column("max_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_checkpoint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("through_revision > 0", name="ck_source_prefix_revision"),
    )
    op.create_index("ix_source_prefix_scope", _TABLE, ["consumer_id", "through_revision"])
    _progress_check(False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_source_prefix_immutable() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN RAISE EXCEPTION 'source prefix history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            f"CREATE TRIGGER source_prefix_immutable BEFORE UPDATE OR DELETE ON {_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION aris_source_prefix_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER source_prefix_no_truncate BEFORE TRUNCATE ON {_TABLE} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_source_prefix_immutable()"
        )
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER source_prefix_{operation.lower()} BEFORE {operation} ON "
                f"{_TABLE} BEGIN SELECT RAISE(ABORT,'source prefix history is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported source prefix database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.select(sa.func.count()).select_from(sa.table(_TABLE))
    ) or connection.scalar(
        sa.text("SELECT count(*) FROM usage_coverage_build_steps WHERE revision>4096")
    ):
        raise RuntimeError("source prefix history prevents downgrade")
    _progress_check(True)
    op.drop_table(_TABLE)
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_source_prefix_immutable()")
