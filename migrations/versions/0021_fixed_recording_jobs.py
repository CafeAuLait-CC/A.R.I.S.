"""Fixed-request, recoverable internal recording jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_recording_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("canonical_request", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "recording_id", sa.String(36), sa.ForeignKey("usage_run_recordings.id"), unique=True
        ),
        sa.Column("recording_fingerprint", sa.String(64)),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','blocked','failed')",
            name="ck_recording_job_state",
        ),
        sa.CheckConstraint(
            "version >= 1 AND attempts >= 0 AND attempts <= 20", name="ck_recording_job_counters"
        ),
        sa.CheckConstraint(
            "length(request_fingerprint)=64 AND length(canonical_request)>0 "
            "AND length(canonical_request)<=16384",
            name="ck_recording_job_input",
        ),
        sa.CheckConstraint(
            "updated_at>=created_at AND available_at>=created_at", name="ck_recording_job_times"
        ),
        sa.CheckConstraint(
            "(state='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND attempts>0 AND lease_expires_at>updated_at) OR "
            "(state<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_recording_job_lease",
        ),
        sa.CheckConstraint(
            "(state IN ('completed','blocked','failed') AND finished_at IS NOT NULL "
            "AND finished_at=updated_at) OR "
            "(state IN ('queued','running') AND finished_at IS NULL)",
            name="ck_recording_job_finish",
        ),
        sa.CheckConstraint(
            "(state='completed' AND recording_id IS NOT NULL AND recording_id=id "
            "AND recording_fingerprint IS NOT NULL AND length(recording_fingerprint)=64) OR "
            "(state<>'completed' AND recording_id IS NULL AND recording_fingerprint IS NULL)",
            name="ck_recording_job_result",
        ),
    )
    op.create_index(
        "ix_recording_jobs_ready",
        "usage_recording_jobs",
        ["state", "available_at", "lease_expires_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION aris_recording_job_history_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ "
            "BEGIN IF TG_OP='DELETE' OR TG_OP='TRUNCATE' THEN "
            "RAISE EXCEPTION 'recording job history is retained' USING ERRCODE='23000'; END IF; "
            "IF NEW.id IS DISTINCT FROM OLD.id OR "
            "NEW.canonical_request IS DISTINCT FROM OLD.canonical_request "
            "OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint "
            "OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN "
            "RAISE EXCEPTION 'recording job input is immutable' USING ERRCODE='23000'; END IF; "
            "RETURN NEW; END $$"
        )
        op.execute(
            "CREATE TRIGGER recording_job_history_guard BEFORE UPDATE OR DELETE "
            "ON usage_recording_jobs FOR EACH ROW "
            "EXECUTE FUNCTION aris_recording_job_history_guard()"
        )
        op.execute(
            "CREATE TRIGGER recording_jobs_no_truncate BEFORE TRUNCATE "
            "ON usage_recording_jobs FOR EACH STATEMENT "
            "EXECUTE FUNCTION aris_recording_job_history_guard()"
        )
    elif dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER recording_jobs_no_delete BEFORE DELETE ON usage_recording_jobs "
            "BEGIN SELECT RAISE(ABORT,'recording job history is retained'); END"
        )
        op.execute(
            "CREATE TRIGGER recording_jobs_input_guard BEFORE UPDATE ON usage_recording_jobs "
            "WHEN NEW.id IS NOT OLD.id OR NEW.canonical_request IS NOT OLD.canonical_request "
            "OR NEW.request_fingerprint IS NOT OLD.request_fingerprint "
            "OR NEW.created_at IS NOT OLD.created_at "
            "BEGIN SELECT RAISE(ABORT,'recording job input is immutable'); END"
        )
    else:
        raise RuntimeError("unsupported recording job database")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.select(sa.func.count()).select_from(sa.table("usage_recording_jobs"))):
        raise RuntimeError("recording job history prevents downgrade")
    op.drop_table("usage_recording_jobs")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION aris_recording_job_history_guard()")
