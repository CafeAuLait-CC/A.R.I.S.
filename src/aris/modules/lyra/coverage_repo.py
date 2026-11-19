"""Lyra-owned bounded immutable processing coverage storage."""

import hashlib
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.coverage_schemas import MAX_COVERAGE_BYTES, StoredProcessingCoverage
from aris.modules.lyra.models import UsageCoverageManifestRow
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class ProcessingCoverageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.coverage.lock\x00" + id.bytes).digest()[:8], signed=True
            )
            self.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def get(self, id: UUID) -> StoredProcessingCoverage | None:
        row = (
            self.session.execute(
                select(*UsageCoverageManifestRow.__table__.c).where(
                    UsageCoverageManifestRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_manifest"]
            if len(raw.encode()) > MAX_COVERAGE_BYTES:
                raise ValueError("oversized coverage manifest")
            value = StoredProcessingCoverage.model_validate_json(raw)
            analysis, request = value.analysis, value.analysis.request
            if canonical_trace(value) != raw or (
                str(request.id),
                str(value.event_id),
                str(request.attestation_id),
                analysis.attestation.consumer_id,
                request.ledger.node_id,
                request.expected_configuration_fingerprint,
                request.ledger.revision,
                request.start_at,
                request.end_at,
                value.created_at,
                value.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["attestation_id"],
                row["consumer_id"],
                row["node_id"],
                row["configuration_fingerprint"],
                row["ledger_revision"],
                utc(row["start_at"]),
                utc(row["end_at"]),
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("coverage manifest metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored processing coverage is inconsistent", 503
            ) from None

    def append(self, value: StoredProcessingCoverage) -> None:
        analysis, request = value.analysis, value.analysis.request
        self.session.add(
            UsageCoverageManifestRow(
                id=str(request.id),
                event_id=str(value.event_id),
                attestation_id=str(request.attestation_id),
                consumer_id=analysis.attestation.consumer_id,
                node_id=request.ledger.node_id,
                configuration_fingerprint=request.expected_configuration_fingerprint,
                ledger_revision=request.ledger.revision,
                start_at=request.start_at,
                end_at=request.end_at,
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_manifest=canonical_trace(value),
            )
        )
