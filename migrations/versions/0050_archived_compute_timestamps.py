"""Project existing component times without changing archived packet fingerprints."""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None
TABLE = "agent_message_compute_times"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "message_row_id",
            sa.String(36),
            sa.ForeignKey("agent_messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    messages = sa.table(
        "agent_messages", sa.column("id"), sa.column("payload", sa.JSON), sa.column("message_kind")
    )
    timestamp = messages.c.payload["resources"]["compute"]["observed_at"].as_string()
    if op.get_bind().dialect.name == "postgresql":
        value = sa.cast(timestamp, sa.DateTime(timezone=True))
    else:
        # Vega persists ISO 8601 UTC. Match SQLAlchemy's SQLite DateTime form,
        # which includes six fractional digits and a space before the time.
        whole_second = sa.func.length(timestamp) == 20
        value = sa.case(
            (
                sa.and_(
                    sa.func.substr(timestamp, -1, 1) == "Z",
                    sa.or_(whole_second, sa.func.length(timestamp) == 27),
                ),
                sa.func.replace(sa.func.substr(timestamp, 1, 19), "T", " ")
                + sa.case((whole_second, ".000000"), else_=sa.func.substr(timestamp, 20, 7)),
            ),
            else_=None,
        )
    times = sa.table(TABLE, sa.column("message_row_id"), sa.column("observed_at"))
    op.execute(
        times.insert().from_select(
            ("message_row_id", "observed_at"),
            sa.select(messages.c.id, value).where(
                messages.c.message_kind == "telemetry", value.is_not(None)
            ),
        )
    )


def downgrade() -> None:
    op.drop_table(TABLE)
