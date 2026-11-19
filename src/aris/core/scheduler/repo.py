"""Owned scheduled-job SQL; definitions and terminal attempts are historical records."""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.scheduler.models import ScheduledJobRow, ScheduledJobRunRow
from aris.core.scheduler.schemas import (
    MAX_TASK_BYTES,
    ScheduledTask,
    StoredScheduledTask,
    StoredTaskRun,
    canonical,
)
from aris.core.time import utc


def inconsistent() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored scheduled task is inconsistent", 503)


class SchedulerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_key(self, key: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            number = int.from_bytes(
                hashlib.sha256(b"aris.scheduler.key\x00" + key.encode()).digest()[:8], signed=True
            )
            self.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})

    def get(self, id: UUID, *, lock: bool = False) -> StoredScheduledTask | None:
        statement = select(*ScheduledJobRow.__table__.c).where(ScheduledJobRow.id == str(id))
        if lock:
            statement = statement.with_for_update()
        row = self.session.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        try:
            raw = row["canonical_request"]
            if len(raw.encode()) > MAX_TASK_BYTES:
                raise ValueError("oversized task")
            request = ScheduledTask.model_validate_json(raw)
            if canonical(request.model_dump(mode="json")) != raw or (
                str(request.id),
                request.key,
                request.kind,
                str(request.owner_id),
                request.scope.type,
                request.scope.id,
                request.scheduled_for,
                request.fingerprint,
            ) != (
                row["id"],
                row["job_key"],
                row["kind"],
                row["owner_id"],
                row["scope_type"],
                row["scope_id"],
                utc(row["scheduled_for"]),
                row["fingerprint"],
            ):
                raise ValueError("task definition metadata differs")
            result = (
                json.loads(row["canonical_result"]) if row["canonical_result"] is not None else None
            )
            if result is not None and (
                canonical(result) != row["canonical_result"]
                or len(row["canonical_result"].encode()) > MAX_TASK_BYTES
            ):
                raise ValueError("task result differs")
            return StoredScheduledTask(
                request=request,
                state=row["state"],
                attempts=row["attempts"],
                created_at=utc(row["created_at"]),
                updated_at=utc(row["updated_at"]),
                available_at=utc(row["available_at"]),
                run_id=row["last_run_id"],
                lease_token=row["lease_token"],
                lease_expires_at=utc(row["lease_expires_at"]) if row["lease_expires_at"] else None,
                result=result,
                error_code=row["error_code"],
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def append(self, request: ScheduledTask, now: datetime) -> None:
        self.session.add(
            ScheduledJobRow(
                id=str(request.id),
                job_key=request.key,
                kind=request.kind,
                owner_id=str(request.owner_id),
                scope_type=request.scope.type,
                scope_id=request.scope.id,
                scheduled_for=request.scheduled_for,
                created_at=now,
                canonical_request=canonical(request.model_dump(mode="json")),
                fingerprint=request.fingerprint,
                state="pending",
                attempts=0,
                available_at=max(now, request.scheduled_for),
                updated_at=now,
            )
        )

    def candidate(self, kinds: tuple[str, ...], now: datetime) -> StoredScheduledTask | None:
        row = ScheduledJobRow
        id = self.session.scalar(
            select(row.id)
            .where(
                row.kind.in_(kinds),
                or_(
                    and_(row.state.in_(("pending", "retry")), row.available_at <= now),
                    and_(row.state == "running", row.lease_expires_at <= now),
                ),
            )
            .order_by(row.available_at, row.scheduled_for, row.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self.get(UUID(id)) if id is not None else None

    def update(self, id: UUID, **values: Any) -> None:
        self.session.execute(
            update(ScheduledJobRow).where(ScheduledJobRow.id == str(id)).values(**values)
        )

    def append_run(
        self, id: UUID, job: StoredScheduledTask, token: UUID, now: datetime, expires: datetime
    ) -> None:
        self.session.add(
            ScheduledJobRunRow(
                id=str(id),
                job_id=str(job.request.id),
                attempt=job.attempts + 1,
                lease_token=str(token),
                started_at=now,
                lease_expires_at=expires,
                state="running",
            )
        )

    def run(self, id: UUID) -> StoredTaskRun | None:
        row = (
            self.session.execute(
                select(*ScheduledJobRunRow.__table__.c).where(ScheduledJobRunRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_result"]
            result = json.loads(raw) if raw is not None else None
            if raw is not None and (canonical(result) != raw or len(raw.encode()) > MAX_TASK_BYTES):
                raise ValueError("attempt result differs")
            return StoredTaskRun(
                id=row["id"],
                job_id=row["job_id"],
                attempt=row["attempt"],
                lease_token=row["lease_token"],
                started_at=utc(row["started_at"]),
                lease_expires_at=utc(row["lease_expires_at"]),
                finished_at=utc(row["finished_at"]) if row["finished_at"] else None,
                state=row["state"],
                result=result,
                error_code=row["error_code"],
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def finish_run(
        self,
        id: UUID,
        state: str,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.session.execute(
            update(ScheduledJobRunRow)
            .where(ScheduledJobRunRow.id == str(id), ScheduledJobRunRow.state == "running")
            .values(
                state=state,
                finished_at=now,
                canonical_result=canonical(result) if result is not None else None,
                error_code=error,
            )
        )

    def runs(self, job_id: UUID) -> tuple[StoredTaskRun, ...]:
        ids = tuple(
            self.session.scalars(
                select(ScheduledJobRunRow.id)
                .where(ScheduledJobRunRow.job_id == str(job_id))
                .order_by(ScheduledJobRunRow.attempt)
                .limit(11)
            )
        )
        if len(ids) > 10:
            raise inconsistent()
        values = tuple(self.run(UUID(id)) for id in ids)
        if any(value is None for value in values):
            raise inconsistent()
        return tuple(value for value in values if value is not None)
