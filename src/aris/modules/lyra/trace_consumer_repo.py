"""Lyra-owned candidate history; a missing cursor must never reset processing."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageTraceCursorRow, UsageTraceRecordRow
from aris.modules.lyra.trace_consumer_schemas import (
    MAX_TRACE_RECORD_BYTES,
    StoredTraceConsumption,
    UsageTraceConfiguration,
    UsageTracePosition,
    canonical_trace,
)


def inconsistent_trace() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored candidate trace is inconsistent", 503)


class UsageTraceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, kind: str, key: str) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            value = int.from_bytes(
                hashlib.sha256(
                    b"aris.lyra.trace.consumer.lock\x00" + kind.encode() + b"\x00" + key.encode()
                ).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})
        elif dialect != "sqlite":
            raise RuntimeError("unsupported candidate trace database")

    @staticmethod
    def _record(row: dict[str, Any]) -> StoredTraceConsumption:
        try:
            raw = row["canonical_record"]
            if len(raw.encode()) > MAX_TRACE_RECORD_BYTES:
                raise ValueError("candidate trace record exceeds its size budget")
            record = StoredTraceConsumption.model_validate(json.loads(raw))
            position = record.position
            if canonical_trace(record) != raw or (
                str(record.request.id),
                str(record.event_id),
                position.configuration.consumer_id,
                position.revision,
                position.after_seq,
                len(record.sources),
                record.created_at,
                record.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["consumer_id"],
                row["revision"],
                row["after_seq"],
                row["source_count"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("candidate trace metadata differs from its immutable record")
            return record
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_trace() from None

    def get(self, record_id: UUID) -> StoredTraceConsumption | None:
        row = (
            self.session.execute(
                select(*UsageTraceRecordRow.__table__.c).where(
                    UsageTraceRecordRow.id == str(record_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._record(dict(row)) if row is not None else None

    def position(self, configuration: UsageTraceConfiguration) -> UsageTracePosition:
        cursor = (
            self.session.execute(
                select(*UsageTraceCursorRow.__table__.c).where(
                    UsageTraceCursorRow.consumer_id == configuration.consumer_id
                )
            )
            .mappings()
            .one_or_none()
        )
        row = (
            self.session.execute(
                select(*UsageTraceRecordRow.__table__.c)
                .where(UsageTraceRecordRow.consumer_id == configuration.consumer_id)
                .order_by(UsageTraceRecordRow.revision.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if cursor is None and row is None:
            return UsageTracePosition(
                configuration=configuration, revision=0, after_seq=configuration.initial_after_seq
            )
        if cursor is None or row is None:
            raise inconsistent_trace()
        record = self._record(dict(row))
        position = record.position
        if position.configuration != configuration:
            raise DomainError(
                "CONFLICT", "Candidate trace configuration requires a new stream", 409
            )
        if (
            cursor["record_id"],
            cursor["revision"],
            cursor["after_seq"],
            cursor["fingerprint"],
        ) != (str(position.record_id), position.revision, position.after_seq, position.fingerprint):
            raise inconsistent_trace()
        if record.before.record_id is not None:
            parent = self.get(record.before.record_id)
            if parent is None or parent.position != record.before:
                raise inconsistent_trace()
        return position

    def source_records(
        self,
        consumer_id: str,
        *,
        through_revision: int,
        first_seq: int,
        last_seq: int,
        after_revision: int,
    ) -> tuple[StoredTraceConsumption, ...]:
        rows = (
            self.session.execute(
                select(*UsageTraceRecordRow.__table__.c)
                .where(
                    UsageTraceRecordRow.consumer_id == consumer_id,
                    UsageTraceRecordRow.revision > after_revision,
                    UsageTraceRecordRow.revision <= through_revision,
                    UsageTraceRecordRow.after_seq >= first_seq,
                    UsageTraceRecordRow.after_seq - UsageTraceRecordRow.source_count < last_seq,
                )
                .order_by(UsageTraceRecordRow.revision)
                .limit(8)
            )
            .mappings()
            .all()
        )
        return tuple(self._record(dict(row)) for row in rows)

    def append(self, record: StoredTraceConsumption) -> None:
        before, after = record.before, record.position
        self.session.add(
            UsageTraceRecordRow(
                id=str(record.request.id),
                event_id=str(record.event_id),
                consumer_id=before.configuration.consumer_id,
                revision=after.revision,
                after_seq=after.after_seq,
                source_count=len(record.sources),
                created_at=record.created_at,
                fingerprint=record.fingerprint,
                canonical_record=canonical_trace(record),
            )
        )
        self.session.flush()
        if before.revision == 0:
            self.session.add(
                UsageTraceCursorRow(
                    consumer_id=before.configuration.consumer_id,
                    record_id=str(record.request.id),
                    revision=after.revision,
                    after_seq=after.after_seq,
                    fingerprint=record.fingerprint,
                )
            )
        else:
            changed = self.session.execute(
                update(UsageTraceCursorRow)
                .where(
                    UsageTraceCursorRow.consumer_id == before.configuration.consumer_id,
                    UsageTraceCursorRow.record_id == str(before.record_id),
                    UsageTraceCursorRow.revision == before.revision,
                    UsageTraceCursorRow.after_seq == before.after_seq,
                    UsageTraceCursorRow.fingerprint == before.fingerprint,
                )
                .values(
                    record_id=str(record.request.id),
                    revision=after.revision,
                    after_seq=after.after_seq,
                    fingerprint=record.fingerprint,
                )
                .returning(UsageTraceCursorRow.consumer_id)
            ).scalar_one_or_none()
            if changed != before.configuration.consumer_id:
                raise DomainError("CONFLICT", "Candidate trace cursor changed before append", 409)
