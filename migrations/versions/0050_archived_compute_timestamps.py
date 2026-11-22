"""Project existing component times without changing archived packet fingerprints."""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None
TABLE = "agent_messages"
COLUMN = "compute_observed_at"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    messages = sa.table(
        TABLE, sa.column("payload", sa.JSON), sa.column("message_kind"), sa.column(COLUMN)
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
    op.execute(
        messages.update().where(messages.c.message_kind == "telemetry").values({COLUMN: value})
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
