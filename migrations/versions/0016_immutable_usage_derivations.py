"""Preserve bounded candidate evidence without posting or activating usage charges."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_derivations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("domain_events.event_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("identity_revision", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("result_fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("canonical_result", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_count >= 1 AND source_count <= 1024", name="ck_usage_derivation_sources"
        ),
        sa.CheckConstraint("identity_revision > 0", name="ck_usage_derivation_identity"),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_usage_derivation_fingerprints",
        ),
        sa.CheckConstraint(
            "length(canonical_request) > 0 AND length(canonical_request) <= 262144",
            name="ck_usage_derivation_request",
        ),
        sa.CheckConstraint(
            "length(canonical_result) > 0 AND length(canonical_result) <= 4194304",
            name="ck_usage_derivation_result",
        ),
        sa.CheckConstraint("created_at >= as_of", name="ck_usage_derivation_time"),
    )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_usage_derivation_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'usage derivation is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER usage_derivation_immutable BEFORE UPDATE OR DELETE "
            "ON usage_derivations FOR EACH ROW EXECUTE FUNCTION aris_usage_derivation_immutable()"
        )
        op.execute(
            "CREATE TRIGGER usage_derivation_no_truncate BEFORE TRUNCATE ON usage_derivations "
            "FOR EACH STATEMENT EXECUTE FUNCTION aris_usage_derivation_immutable()"
        )
    elif connection.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                "CREATE TRIGGER usage_derivation_no_"
                + operation.lower()
                + " BEFORE "
                + operation
                + " ON usage_derivations BEGIN "
                "SELECT RAISE(ABORT,'usage derivation is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported usage derivation database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(sa.table("usage_derivations"))):
        raise RuntimeError("usage derivation history prevents downgrade")
    op.drop_table("usage_derivations")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_usage_derivation_immutable()")
