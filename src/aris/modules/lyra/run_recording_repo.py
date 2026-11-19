"""Lyra-owned immutable closed-run operation binding."""

import hashlib
import json
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import ClosedRunRecordingRow
from aris.modules.lyra.partition_recording_schemas import StoredPartitionRecording
from aris.modules.lyra.run_recording_schemas import (
    MAX_RUN_RECORD_BYTES,
    StoredClosedRunRecording,
    canonical_run_recording,
)

StoredRunRecording = StoredClosedRunRecording | StoredPartitionRecording
_recording_adapter: TypeAdapter[StoredRunRecording] = TypeAdapter(StoredRunRecording)


class ClosedRunRecordingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, operation_id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.closed_run.lock\x00" + operation_id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def get_any(self, operation_id: UUID) -> StoredRunRecording | None:
        row = (
            self.session.execute(
                select(*ClosedRunRecordingRow.__table__.c).where(
                    ClosedRunRecordingRow.id == str(operation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_record"]
            if len(raw.encode()) > MAX_RUN_RECORD_BYTES:
                raise ValueError("oversized closed-run record")
            result = _recording_adapter.validate_python(json.loads(raw))
            if canonical_run_recording(result) != raw or (
                str(result.request.id),
                str(result.event_id),
                str(result.request.derivation_id),
                str(result.ledger.id),
                result.ledger.head.node_id,
                result.proof.run.id
                if isinstance(result, StoredClosedRunRecording)
                else result.proof.parent_run.id,
                result.created_at,
                result.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["derivation_id"],
                row["ledger_operation_id"],
                row["node_id"],
                row["run_id"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("closed-run record metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored closed-run recording is inconsistent", 503
            ) from None

    def get(self, operation_id: UUID) -> StoredClosedRunRecording | None:
        result = self.get_any(operation_id)
        if result is not None and not isinstance(result, StoredClosedRunRecording):
            raise DomainError("CONFLICT", "Recording operation uses a different request kind", 409)
        return result

    def get_partition(self, operation_id: UUID) -> StoredPartitionRecording | None:
        result = self.get_any(operation_id)
        if result is not None and not isinstance(result, StoredPartitionRecording):
            raise DomainError("CONFLICT", "Recording operation uses a different request kind", 409)
        return result

    def append(self, record: StoredRunRecording) -> None:
        self.session.add(
            ClosedRunRecordingRow(
                id=str(record.request.id),
                event_id=str(record.event_id),
                derivation_id=str(record.request.derivation_id),
                ledger_operation_id=str(record.ledger.id),
                node_id=record.ledger.head.node_id,
                run_id=record.proof.run.id
                if isinstance(record, StoredClosedRunRecording)
                else record.proof.parent_run.id,
                created_at=record.created_at,
                fingerprint=record.fingerprint,
                canonical_record=canonical_run_recording(record),
            )
        )
