"""Remove the obsolete format marker from current resource projections only."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def projections() -> sa.TableClause:
    return sa.table(
        "node_resources",
        sa.column("node_id", sa.String(128)),
        sa.column("observation", sa.JSON()),
    )


def upgrade() -> None:
    table = projections()
    bind = op.get_bind()
    for node_id, stored in bind.execute(sa.select(table.c.node_id, table.c.observation)):
        observation = dict(stored)
        if "schema_version" not in observation:
            continue
        if observation.pop("schema_version") != "aris.resource-observation/v1":
            raise RuntimeError("unrecognized current resource projection")
        bind.execute(
            table.update().where(table.c.node_id == node_id).values(observation=observation)
        )


def downgrade() -> None:
    table = projections()
    bind = op.get_bind()
    for node_id, stored in bind.execute(sa.select(table.c.node_id, table.c.observation)):
        observation = {**stored, "schema_version": "aris.resource-observation/v1"}
        bind.execute(
            table.update().where(table.c.node_id == node_id).values(observation=observation)
        )
