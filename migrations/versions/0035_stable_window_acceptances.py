"""Immutable first accepted proofs for stable observed residency windows."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_stable_window_acceptances",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("layout_key", sa.String(64), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("identity_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_record", sa.Text(), nullable=False),
        sa.CheckConstraint("length(key) = 64", name="ck_stable_acceptance_key"),
        sa.CheckConstraint("length(layout_key) = 64", name="ck_stable_acceptance_layout"),
        sa.CheckConstraint("length(run_id) = 64", name="ck_stable_acceptance_run"),
        sa.CheckConstraint(
            "length(configuration_fingerprint) = 64", name="ck_stable_acceptance_config"
        ),
        sa.CheckConstraint("length(fingerprint) = 64", name="ck_stable_acceptance_fingerprint"),
        sa.CheckConstraint("identity_revision >= 1", name="ck_stable_acceptance_identity"),
        sa.CheckConstraint(
            "length(canonical_record) > 0 AND length(canonical_record) <= 4194304",
            name="ck_stable_acceptance_size",
        ),
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_stable_acceptance_immutable() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'stable window evidence is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER stable_acceptance_immutable BEFORE UPDATE OR DELETE "
            "ON usage_stable_window_acceptances FOR EACH ROW "
            "EXECUTE FUNCTION aris_stable_acceptance_immutable()"
        )
        op.execute(
            "CREATE TRIGGER stable_acceptance_no_truncate BEFORE TRUNCATE "
            "ON usage_stable_window_acceptances FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_stable_acceptance_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER stable_acceptance_no_{operation.lower()} BEFORE {operation} "
                "ON usage_stable_window_acceptances BEGIN "
                "SELECT RAISE(ABORT,'stable window evidence is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported stable acceptance database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_stable_window_acceptances"))
    ):
        raise RuntimeError("accepted stable window history prevents downgrade")
    op.drop_table("usage_stable_window_acceptances")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_stable_acceptance_immutable()")
