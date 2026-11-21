"""All recording job kinds bind their common immutable ledger operation."""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TABLE = "usage_recording_jobs"
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _replace(target: str, name: str) -> None:
    connection = op.get_bind()
    triggers = []
    if connection.dialect.name == "sqlite":
        triggers = list(
            connection.scalars(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='usage_recording_jobs'"
                )
            )
        )
    constraints = sa.inspect(connection).get_foreign_keys(_TABLE)
    prior = [item for item in constraints if item["constrained_columns"] == ["recording_id"]]
    if len(prior) != 1:
        raise RuntimeError("recording job result foreign key differs")
    old = prior[0]
    old_name = old["name"] or f"fk_{_TABLE}_recording_id_{old['referred_table']}"
    with op.batch_alter_table(_TABLE, naming_convention=_NAMING) as batch:
        batch.drop_constraint(old_name, type_="foreignkey")
        batch.create_foreign_key(name, target, ["recording_id"], ["id"])
    for statement in triggers:
        op.execute(statement)


def upgrade() -> None:
    _replace("usage_ledger_operations", "fk_recording_jobs_operation")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM usage_recording_jobs "
            'WHERE canonical_request LIKE \'%"kind":"stable_window"%\''
        )
    ):
        raise RuntimeError("stable recording jobs prevent downgrade")
    _replace("usage_run_recordings", "fk_recording_jobs_closed_record")
