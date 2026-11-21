"""A bounded derived index of stable operations, not a new measured-time authority."""

import json
import re
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None
_NAMESPACE = UUID("a45d60c0-bf4b-54aa-bd25-27f655bc78bc")


def upgrade() -> None:
    op.create_table(
        "usage_stable_recording_links",
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey("usage_ledger_operations.id"),
            primary_key=True,
        ),
        sa.Column(
            "window_key",
            sa.String(64),
            sa.ForeignKey("usage_stable_window_acceptances.key"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "entry_id", sa.String(36), sa.ForeignKey("usage_ledger_entries.id"), nullable=False
        ),
    )
    op.create_index(
        "ix_stable_recording_link_entry",
        "usage_stable_recording_links",
        ["entry_id", "operation_id"],
    )
    connection = op.get_bind()
    accepted = sa.table(
        "usage_stable_window_acceptances", sa.column("key"), sa.column("configuration_fingerprint")
    )
    operations = sa.table(
        "usage_ledger_operations",
        sa.column("id"),
        sa.column("canonical_request"),
        sa.column("canonical_result"),
    )
    target = sa.table(
        "usage_stable_recording_links",
        sa.column("operation_id"),
        sa.column("window_key"),
        sa.column("entry_id"),
    )
    after = ""
    while True:
        keys = connection.execute(
            sa.select(accepted.c.key, accepted.c.configuration_fingerprint)
            .where(accepted.c.key > after)
            .order_by(accepted.c.key)
            .limit(128)
        ).all()
        if not keys:
            break
        bindings = {}
        for key, configuration in keys:
            if re.fullmatch(r"[a-f0-9]{64}", key) is None:
                raise RuntimeError("stable acceptance key is invalid")
            bindings[str(uuid5(_NAMESPACE, "ledger:" + key))] = (key, configuration)
        rows = connection.execute(
            sa.select(operations).where(operations.c.id.in_(bindings))
        ).mappings()
        for row in rows:
            key, configuration = bindings[row["id"]]
            request, receipt = (
                json.loads(row["canonical_request"]),
                json.loads(row["canonical_result"]),
            )
            if (
                request.get("operation") != "post_measurement"
                or request.get("id") != row["id"]
                or request.get("derivation_id") != str(uuid5(_NAMESPACE, "derivation:" + key))
                or request.get("expected_configuration_fingerprint") != configuration
                or receipt.get("id") != row["id"]
                or len(receipt.get("entries", ())) != 1
            ):
                raise RuntimeError("stable operation differs from its accepted binding")
            entry_id = str(UUID(receipt["entries"][0]))
            connection.execute(
                sa.insert(target).values(operation_id=row["id"], window_key=key, entry_id=entry_id)
            )
        after = keys[-1][0]
    dialect = connection.dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_stable_link_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'stable recording link is immutable' "
            "USING ERRCODE='23000'; END $$"
        )
        op.execute(
            "CREATE TRIGGER stable_link_immutable BEFORE UPDATE OR DELETE ON "
            "usage_stable_recording_links FOR EACH ROW EXECUTE FUNCTION "
            "aris_stable_link_immutable()"
        )
        op.execute(
            "CREATE TRIGGER stable_link_no_truncate BEFORE TRUNCATE ON "
            "usage_stable_recording_links FOR EACH STATEMENT EXECUTE FUNCTION "
            "aris_stable_link_immutable()"
        )
    elif dialect == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER stable_link_no_{operation.lower()} BEFORE {operation} ON "
                "usage_stable_recording_links BEGIN SELECT "
                "RAISE(ABORT,'stable recording link is immutable'); END"
            )
    else:
        raise RuntimeError("unsupported stable recording link database")


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(sa.table("usage_stable_recording_links"))
    ):
        raise RuntimeError("stable recording link history prevents downgrade")
    op.drop_table("usage_stable_recording_links")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_stable_link_immutable()")
