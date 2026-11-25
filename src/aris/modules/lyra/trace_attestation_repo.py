"""Immutable, one-to-one bindings to consumed trace records, owned by Lyra."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageTraceAttestationRow
from aris.modules.lyra.trace_attestation_schemas import (
    MAX_ATTESTATION_BYTES,
    StoredTraceAttestation,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


def inconsistent_attestation() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored source attestation is inconsistent", 503)


class TraceAttestationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, kind: str, key: str) -> None:
        UsageTraceRepository(self.session).lock("attestation_" + kind, key)

    def get(self, id: UUID, *, by_record: bool = False) -> StoredTraceAttestation | None:
        column = UsageTraceAttestationRow.record_id if by_record else UsageTraceAttestationRow.id
        row = (
            self.session.execute(
                select(*UsageTraceAttestationRow.__table__.c).where(column == str(id))
            )
            .mappings()
            .one_or_none()
        )
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row: RowMapping) -> StoredTraceAttestation:
        try:
            raw = row["canonical_record"]
            if len(raw.encode()) > MAX_ATTESTATION_BYTES:
                raise ValueError("source attestation exceeds its storage budget")
            result = StoredTraceAttestation.model_validate(json.loads(raw))
            if canonical_trace(result) != raw or (
                str(result.request.id),
                str(result.event_id),
                str(result.request.record_id),
                str(result.previous_id) if result.previous_id else None,
                result.consumer_id,
                result.revision,
                result.first_seq,
                result.last_seq,
                result.created_at,
                result.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["record_id"],
                row["previous_id"],
                row["consumer_id"],
                row["revision"],
                row["first_seq"],
                row["last_seq"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("source attestation row differs from its immutable record")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_attestation() from None

    def append(self, value: StoredTraceAttestation) -> None:
        self.session.add(
            UsageTraceAttestationRow(
                id=str(value.request.id),
                event_id=str(value.event_id),
                record_id=str(value.request.record_id),
                previous_id=str(value.previous_id) if value.previous_id else None,
                consumer_id=value.consumer_id,
                revision=value.revision,
                first_seq=value.first_seq,
                last_seq=value.last_seq,
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_record=canonical_trace(value),
            )
        )

    def at_revision(self, consumer_id: str, revision: int) -> StoredTraceAttestation | None:
        row = UsageTraceAttestationRow
        value = (
            self.session.execute(
                select(*row.__table__.c).where(
                    row.consumer_id == consumer_id, row.revision == revision
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode(value) if value is not None else None
