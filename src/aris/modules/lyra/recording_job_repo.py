"""Queries and mutable lease projections of fixed Lyra recording jobs."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageRecordingJobRow
from aris.modules.lyra.recording_job_schemas import (
    MAX_RECORDING_JOB_INPUT_BYTES,
    RecordingWorkSelection,
    StoredRecordingJob,
    recording_request_adapter,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class RecordingJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_operation(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.recording_job.lock\x00" + id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def get(self, id: UUID) -> StoredRecordingJob | None:
        row = (
            self.session.execute(
                select(*UsageRecordingJobRow.__table__.c).where(UsageRecordingJobRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_request"]
            if len(raw.encode()) > MAX_RECORDING_JOB_INPUT_BYTES:
                raise ValueError("oversized recording job")
            request = recording_request_adapter.validate_python(json.loads(raw))
            if canonical_trace(request) != raw or str(request.id) != row["id"]:
                raise ValueError("recording job input differs")
            return StoredRecordingJob.model_validate(
                {
                    "request": request,
                    "request_fingerprint": row["request_fingerprint"],
                    **{
                        key: row[key]
                        for key in (
                            "state",
                            "version",
                            "attempts",
                            "lease_token",
                            "last_error_code",
                            "recording_id",
                            "recording_fingerprint",
                        )
                    },
                    **{
                        key: utc(row[key]) if row[key] is not None else None
                        for key in (
                            "created_at",
                            "updated_at",
                            "available_at",
                            "lease_expires_at",
                            "finished_at",
                        )
                    },
                }
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored recording job is inconsistent", 503
            ) from None

    def locked(self, id: UUID) -> UsageRecordingJobRow | None:
        return self.session.scalar(
            select(UsageRecordingJobRow).where(UsageRecordingJobRow.id == str(id)).with_for_update()
        )

    def next(
        self,
        now: datetime,
        *,
        include_partitions: bool = True,
        selection: RecordingWorkSelection | None = None,
    ) -> UsageRecordingJobRow | None:
        statement = select(UsageRecordingJobRow)
        if not include_partitions:
            # The two closed canonical input shapes are unambiguous. Full-run
            # requests contain only UUIDs, hashes, times and numeric revisions;
            # none can contain a nested or caller-supplied partition key.
            statement = statement.where(
                ~UsageRecordingJobRow.canonical_request.contains('"partition":')
            )
        if selection is not None:
            statement = statement.where(
                UsageRecordingJobRow.canonical_request.contains(
                    '"expected_recording_configuration_fingerprint":"'
                    + selection.recording_configuration_fingerprint
                    + '"',
                    autoescape=True,
                ),
                or_(
                    ~UsageRecordingJobRow.canonical_request.contains('"partition":'),
                    UsageRecordingJobRow.canonical_request.contains(
                        '"expected_method_fingerprint":"'
                        + selection.partition_method_fingerprint
                        + '"',
                        autoescape=True,
                    ),
                ),
            )
        return self.session.scalar(
            statement.where(
                or_(
                    and_(
                        UsageRecordingJobRow.state == "queued",
                        UsageRecordingJobRow.available_at <= now,
                    ),
                    and_(
                        UsageRecordingJobRow.state == "running",
                        UsageRecordingJobRow.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(
                UsageRecordingJobRow.available_at,
                UsageRecordingJobRow.created_at,
                UsageRecordingJobRow.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
