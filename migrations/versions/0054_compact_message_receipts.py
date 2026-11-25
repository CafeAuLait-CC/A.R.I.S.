"""Keep original replay acknowledgments in bounded indexed blocks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_archive_states") as batch:
        batch.add_column(
            sa.Column(
                "receipts_compacted_through_seq",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_archive_receipt_compaction_prefix",
            "receipts_compacted_through_seq>=0 AND "
            "receipts_compacted_through_seq<=retired_through_seq",
        )
    op.create_table(
        "agent_receipt_blocks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(128),
            sa.ForeignKey("agent_archive_states.agent_id"),
            nullable=False,
        ),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        sa.Column("packed_receipts", sa.LargeBinary(6272), nullable=False),
        sa.Column("message_ids", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint("agent_id", "first_seq", name="uq_agent_receipt_block_start"),
        sa.CheckConstraint(
            "first_seq>0 AND through_seq>=first_seq AND through_seq-first_seq<128 "
            "AND length(fingerprint)=64 AND length(packed_receipts)=(through_seq-first_seq+1)*49",
            name="ck_agent_receipt_block_shape",
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_agent_receipt_block_message_ids",
            "agent_receipt_blocks",
            ["message_ids"],
            postgresql_using="gin",
            postgresql_ops={"message_ids": "jsonb_path_ops"},
            postgresql_with={"fastupdate": "off"},
        )
    protect_acknowledgments(compaction=True)


def protect_acknowledgments(*, compaction: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        allowed = (
            "IF TG_OP='DELETE' AND EXISTS (SELECT 1 FROM agent_receipt_blocks b "
            "WHERE b.agent_id=OLD.agent_id AND OLD.seq BETWEEN b.first_seq AND b.through_seq "
            "AND b.message_ids->>((OLD.seq-b.first_seq)::integer)=OLD.message_id "
            "AND substring(b.packed_receipts FROM ((OLD.seq-b.first_seq)*49+1)::integer FOR 49)="
            "decode(replace(OLD.message_id,'-',''),'hex')||decode(OLD.fingerprint,'hex')||"
            "decode(CASE WHEN OLD.applied_to_current THEN '01' ELSE '00' END,'hex')) "
            "THEN RETURN OLD; END IF; "
            if compaction
            else ""
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION aris_agent_message_receipt_immutable() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
            + allowed
            + "RAISE EXCEPTION 'agent message receipts are immutable' USING ERRCODE='23000'; END $$"
        )
        if compaction:
            op.execute(
                "CREATE FUNCTION aris_agent_receipt_block_immutable() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'receipt blocks are immutable' "
                "USING ERRCODE='23000'; END $$"
            )
            op.execute(
                "CREATE TRIGGER agent_receipt_block_immutable BEFORE UPDATE OR DELETE "
                "ON agent_receipt_blocks FOR EACH ROW "
                "EXECUTE FUNCTION aris_agent_receipt_block_immutable()"
            )
            op.execute(
                "CREATE TRIGGER agent_receipt_block_no_truncate BEFORE TRUNCATE "
                "ON agent_receipt_blocks FOR EACH STATEMENT "
                "EXECUTE FUNCTION aris_agent_receipt_block_immutable()"
            )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER agent_message_receipt_delete")
        allowed = (
            "WHEN NOT EXISTS (SELECT 1 FROM agent_receipt_blocks b "
            "WHERE b.agent_id=OLD.agent_id AND OLD.seq BETWEEN b.first_seq AND b.through_seq "
            "AND json_extract(b.message_ids,'$['||(OLD.seq-b.first_seq)||']')=OLD.message_id "
            "AND lower(hex(substr(b.packed_receipts,(OLD.seq-b.first_seq)*49+1,49)))="
            "replace(OLD.message_id,'-','')||OLD.fingerprint||"
            "CASE WHEN OLD.applied_to_current THEN '01' ELSE '00' END) "
            if compaction
            else ""
        )
        op.execute(
            "CREATE TRIGGER agent_message_receipt_delete BEFORE DELETE ON agent_message_receipts "
            + allowed
            + "BEGIN SELECT RAISE(ABORT,'agent message receipts are immutable'); END"
        )
        if compaction:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER agent_receipt_block_{operation.lower()} BEFORE {operation} "
                    "ON agent_receipt_blocks BEGIN "
                    "SELECT RAISE(ABORT,'receipt blocks are immutable'); END"
                )
    else:
        raise RuntimeError("unsupported receipt compaction database")


def downgrade() -> None:
    connection = op.get_bind()
    if (
        connection.execute(sa.text("SELECT id FROM agent_receipt_blocks LIMIT 1")).first()
        is not None
        or connection.execute(
            sa.text(
                "SELECT agent_id FROM agent_archive_states "
                "WHERE receipts_compacted_through_seq>0 LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError("Cannot discard compacted Vega replay acknowledgments")
    protect_acknowledgments(compaction=False)
    op.drop_table("agent_receipt_blocks")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_agent_receipt_block_immutable()")
    with op.batch_alter_table("agent_archive_states") as batch:
        batch.drop_constraint("ck_archive_receipt_compaction_prefix", type_="check")
        batch.drop_column("receipts_compacted_through_seq")
