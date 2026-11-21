"""Immutable fixed-boundary plans and one bounded coverage proof per build step."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None
_TABLES = (
    "usage_coverage_build_plans",
    "usage_coverage_build_steps",
    "usage_recording_receipt_order",
)


def upgrade() -> None:
    op.create_table(
        _TABLES[0],
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("consumer_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_plan", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_coverage_build_plans_consumer_id", _TABLES[0], ["consumer_id"])
    op.create_table(
        _TABLES[1],
        sa.Column("plan_id", sa.String(36), sa.ForeignKey(_TABLES[0] + ".id"), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "attestation_id",
            sa.String(36),
            sa.ForeignKey("usage_trace_attestations.id"),
            nullable=False,
        ),
        sa.Column(
            "manifest_id",
            sa.String(36),
            sa.ForeignKey("usage_coverage_manifests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_step", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1 AND revision <= 4096", name="coverage_build_step_revision"
        ),
    )
    op.create_table(
        _TABLES[2],
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("receipt_id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("node_id", "revision", name="uq_recording_receipt_revision"),
        sa.CheckConstraint("revision > 0", name="ck_recording_receipt_revision"),
        sa.CheckConstraint("kind IN ('closed','stable')", name="ck_recording_receipt_kind"),
    )
    connection = op.get_bind()
    target = sa.table(
        _TABLES[2],
        sa.column("kind"),
        sa.column("receipt_id"),
        sa.column("node_id"),
        sa.column("revision"),
    )
    closed = sa.table("usage_run_recordings", sa.column("id"), sa.column("node_id"))
    stable = sa.table("usage_stable_recording_links", sa.column("operation_id"))
    operations = sa.table("usage_ledger_operations", sa.column("id"), sa.column("canonical_result"))
    # All old committed receipts precede every new receipt; initial tie ordering is immaterial.
    # Keyset pages keep the migration independent of the number or duration of source runs.
    for kind in ("closed", "stable"):
        after = ""
        while True:
            query = (
                sa.select(closed.c.id, closed.c.node_id)
                .where(closed.c.id > after)
                .order_by(closed.c.id)
                .limit(128)
                if kind == "closed"
                else sa.select(stable.c.operation_id, operations.c.canonical_result)
                .join(operations, operations.c.id == stable.c.operation_id)
                .where(stable.c.operation_id > after)
                .order_by(stable.c.operation_id)
                .limit(128)
            )
            rows = connection.execute(query).all()
            if not rows:
                break
            for id, raw in rows:
                node = raw if kind == "closed" else json.loads(raw)["head"]["node_id"]
                if not isinstance(node, str) or not 1 <= len(node) <= 128:
                    raise RuntimeError("recording order node is invalid")
                current = connection.scalar(
                    sa.select(sa.func.coalesce(sa.func.max(target.c.revision), 0)).where(
                        target.c.node_id == node
                    )
                )
                connection.execute(
                    sa.insert(target).values(
                        kind=kind, receipt_id=id, node_id=node, revision=current + 1
                    )
                )
            after = rows[-1][0]
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_coverage_build_immutable() RETURNS trigger LANGUAGE "
            "plpgsql AS $$ BEGIN RAISE EXCEPTION 'coverage build history is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        for table in _TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION aris_coverage_build_immutable()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION aris_coverage_build_immutable()"
            )
    elif dialect == "sqlite":
        for table in _TABLES:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT,"
                    "'coverage build history is immutable'); END"
                )
    else:
        raise RuntimeError("unsupported coverage build database")


def downgrade() -> None:
    if any(
        op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(table)))
        for table in _TABLES[:2]
    ):
        raise RuntimeError("coverage build history prevents downgrade")
    for table in reversed(_TABLES):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_coverage_build_immutable()")
