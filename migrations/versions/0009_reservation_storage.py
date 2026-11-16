"""Reservation storage and partial PG exclusion; extension provisioning is explicit."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres and not op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM pg_catalog.pg_extension e "
            "JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace "
            "WHERE e.extname='btree_gist' AND n.nspname='public'"
        )
    ):
        raise RuntimeError("0009 requires pre-provisioned public.btree_gist")
    op.create_table(
        "reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("gpu_uuid", sa.String(128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(256), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("start_at < end_at", name="ck_reservation_window"),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','active','completed',"
            "'cancelled','rejected','reclaiming')",
            name="ck_reservation_status",
        ),
    )
    op.create_index("ix_reservations_owner_id", "reservations", ["owner_id"])
    op.create_index("ix_reservations_gpu_uuid", "reservations", ["gpu_uuid"])
    if postgres:
        # Alembic's create_exclude_constraint helper expects named columns;
        # this constraint intentionally uses an immutable range expression.
        op.execute(
            sa.text(
                "ALTER TABLE reservations ADD CONSTRAINT ex_reservation_gpu_interval "
                "EXCLUDE USING gist (gpu_uuid public.gist_text_ops WITH =, "
                "tstzrange(start_at, end_at, '[)') WITH &&) "
                "WHERE (status IN ('confirmed','active','reclaiming'))"
            )
        )
    op.create_table(
        "reservation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "reservation_id", sa.String(36), sa.ForeignKey("reservations.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("reservation_id", "version", name="uq_reservation_event_version"),
        sa.CheckConstraint("version > 0", name="ck_reservation_event_version"),
    )
    op.create_index(
        "ix_reservation_events_reservation_id", "reservation_events", ["reservation_id"]
    )


def downgrade() -> None:
    for name in ("reservation_events", "reservations"):
        if op.get_bind().scalar(sa.select(sa.func.count()).select_from(sa.table(name))):
            raise RuntimeError("reservation history prevents downgrade")
    op.drop_table("reservation_events")
    op.drop_table("reservations")
