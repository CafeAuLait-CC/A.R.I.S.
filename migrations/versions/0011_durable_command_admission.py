"""Persist metadata-only intake, verified consent and fenced command jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("commands") as batch:
        batch.add_column(sa.Column("execution_not_after", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("execution_deadline", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("failure_code", sa.String(64)))
        batch.drop_constraint("ck_command_state", type_="check")
        batch.create_check_constraint(
            "ck_command_state",
            "state IN ('pending','awaiting_confirmation','queued','succeeded','expired',"
            "'cancelled','failed')",
        )
        batch.create_check_constraint(
            "ck_command_execution_deadline",
            "execution_deadline IS NULL OR (execution_not_after IS NOT NULL AND "
            "execution_deadline <= execution_not_after)",
        )
        batch.create_check_constraint(
            "ck_command_queued_deadline", "state <> 'queued' OR execution_deadline IS NOT NULL"
        )
        batch.create_check_constraint(
            "ck_command_failure", "state <> 'failed' OR failure_code IS NOT NULL"
        )
    op.create_table(
        "command_admissions",
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("definition_fingerprint", sa.String(64), nullable=False),
        sa.Column("max_queue_delay_seconds", sa.Integer(), nullable=False),
        sa.Column("confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("confirmation_key_id", sa.String(64), nullable=False),
        sa.Column("confirmation_key_tag", sa.String(64)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("execution_deadline", sa.DateTime(timezone=True)),
        sa.Column("verified_fingerprint", sa.String(64)),
        sa.Column("verification_kind", sa.String(16)),
        sa.CheckConstraint(
            "max_queue_delay_seconds > 0 AND max_queue_delay_seconds <= 600",
            name="ck_admission_delay",
        ),
        sa.CheckConstraint(
            "NOT confirmation_required OR confirmation_key_tag IS NOT NULL", name="ck_admission_key"
        ),
        sa.CheckConstraint(
            "(verified_at IS NULL AND execution_deadline IS NULL AND verified_fingerprint IS "
            "NULL AND verification_kind IS NULL) OR (verified_at IS NOT NULL AND "
            "execution_deadline IS NOT NULL AND verified_at < execution_deadline AND "
            "verified_fingerprint IS NOT NULL AND ((confirmation_required AND "
            "verification_kind = 'proof') OR (NOT confirmation_required AND "
            "verification_kind = 'not_required')))",
            name="ck_admission_verification",
        ),
    )
    op.create_table(
        "command_intake_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("key_sha256", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("subject_fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_status", sa.Integer()),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "key_sha256", name="uq_intake_actor_receipt"),
        sa.CheckConstraint("kind IN ('submit','confirm','withdraw')", name="ck_intake_kind"),
        sa.CheckConstraint("state IN ('receiving','accepted','rejected')", name="ck_intake_state"),
        sa.CheckConstraint("attempts > 0", name="ck_intake_attempts"),
        sa.CheckConstraint(
            "(state = 'receiving' AND error_code IS NULL AND error_status IS NULL) OR (state "
            "= 'accepted' AND command_id IS NOT NULL AND error_code IS NULL AND error_status "
            "IS NULL) OR (state = 'rejected' AND error_code IS NOT NULL AND error_status >= "
            "400 AND error_status <= 599)",
            name="ck_intake_outcome",
        ),
    )
    op.create_table(
        "command_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("command_id", sa.String(36), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("command_id", name="uq_command_job_command"),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','expired','cancelled')",
            name="ck_command_job_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_command_job_attempts"),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL) OR (state = 'queued' AND lease_token IS NULL AND "
            "lease_expires_at IS NULL AND completed_at IS NULL) OR (state IN ('succeeded',"
            "'failed','expired','cancelled') AND lease_token IS NULL AND lease_expires_at IS "
            "NULL AND completed_at IS NOT NULL)",
            name="ck_command_job_lease",
        ),
    )
    op.create_index(
        "ix_command_jobs_ready", "command_jobs", ["state", "available_at", "lease_expires_at"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("command_intake_receipts", "command_admissions", "command_jobs"):
        table = sa.table(name)
        if connection.scalar(sa.select(sa.func.count()).select_from(table)):
            raise RuntimeError("durable command history prevents downgrade")
    if connection.scalar(
        sa.text(
            "SELECT count(*) FROM commands WHERE execution_not_after IS NOT NULL OR "
            "execution_deadline IS NOT NULL OR failure_code IS NOT NULL OR state IN "
            "('queued','cancelled','failed')"
        )
    ):
        raise RuntimeError("durable command history prevents downgrade")
    op.drop_index("ix_command_jobs_ready", table_name="command_jobs")
    op.drop_table("command_jobs")
    op.drop_table("command_intake_receipts")
    op.drop_table("command_admissions")
    with op.batch_alter_table("commands") as batch:
        for name in (
            "ck_command_execution_deadline",
            "ck_command_queued_deadline",
            "ck_command_failure",
            "ck_command_state",
        ):
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(
            "ck_command_state", "state IN ('pending','awaiting_confirmation','succeeded','expired')"
        )
        batch.drop_column("failure_code")
        batch.drop_column("execution_deadline")
        batch.drop_column("execution_not_after")
