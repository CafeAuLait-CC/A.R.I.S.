import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.derivation_schemas import (
    MAX_DERIVATION_INPUT_BYTES,
    MAX_DERIVATION_RESULT_BYTES,
    StoredUsageDerivation,
    UsageDerivationArtifact,
    UsageDerivationReceipt,
    UsageDerivationRequest,
    canonical_derivation,
    derivation_fingerprint,
)
from aris.modules.lyra.models import UsageDerivation


class UsageDerivationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, record_id: UUID) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.derivation.lock\x00" + record_id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        elif dialect != "sqlite":
            raise RuntimeError("unsupported derivation database")

    @staticmethod
    def _stored(row: dict[str, Any], *, duplicate: bool) -> StoredUsageDerivation:
        try:
            raw_input, raw_result = row["canonical_request"], row["canonical_result"]
            if (
                len(raw_input.encode()) > MAX_DERIVATION_INPUT_BYTES
                or len(raw_result.encode()) > MAX_DERIVATION_RESULT_BYTES
                or derivation_fingerprint(raw_input, result=False) != row["request_fingerprint"]
                or derivation_fingerprint(raw_result, result=True) != row["result_fingerprint"]
            ):
                raise ValueError("invalid derivation fingerprints or size")
            request = UsageDerivationRequest.model_validate(json.loads(raw_input))
            artifact = UsageDerivationArtifact.model_validate(json.loads(raw_result))
            if (
                canonical_derivation(request) != raw_input
                or canonical_derivation(artifact) != raw_result
                or artifact.request != request
                or str(request.id) != row["id"]
                or len(request.sources) != row["source_count"]
                or request.identity_revision != row["identity_revision"]
                or request.as_of != utc(row["as_of"])
                or (request.sources[0].agent_id, request.sources[0].node_id)
                != (row["agent_id"], row["node_id"])
                or utc(row["created_at"]) < request.as_of
            ):
                raise ValueError("stored derivation metadata differs")
            return StoredUsageDerivation(
                artifact=artifact,
                receipt=UsageDerivationReceipt(
                    id=row["id"],
                    event_id=row["event_id"],
                    duplicate=duplicate,
                    created_at=utc(row["created_at"]),
                    source_count=row["source_count"],
                    request_fingerprint=row["request_fingerprint"],
                    result_fingerprint=row["result_fingerprint"],
                ),
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored usage derivation is inconsistent", 503
            ) from None

    def get(self, record_id: UUID, *, duplicate: bool = True) -> StoredUsageDerivation | None:
        row = (
            self.session.execute(
                select(*UsageDerivation.__table__.c).where(UsageDerivation.id == str(record_id))
            )
            .mappings()
            .one_or_none()
        )
        return self._stored(dict(row), duplicate=duplicate) if row is not None else None

    def append(self, row: UsageDerivation) -> None:
        self.session.add(row)
