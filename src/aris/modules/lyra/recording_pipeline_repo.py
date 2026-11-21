"""Immutable batch bindings; raw source and plan repositories keep their ownership."""

import hashlib
import json
from typing import Literal, overload
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import (
    UsageRecordingPipelineBatchRow,
    UsageResidencySegmentSetRow,
    UsageTraceAttestationRow,
    UsageTraceRecordRow,
)
from aris.modules.lyra.recording_pipeline_schemas import (
    MAX_PIPELINE_BATCH_BYTES,
    StoredRecordingPipelineBatch,
)
from aris.modules.lyra.stable_pipeline_schemas import StoredStableRecordingPipelineBatch
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class RecordingPipelineRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.pipeline.batch\x00" + id.bytes).digest()[:8], signed=True
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    @overload
    def get(
        self, id: UUID, *, mode: Literal["closed"] = "closed"
    ) -> StoredRecordingPipelineBatch | None: ...

    @overload
    def get(
        self, id: UUID, *, mode: Literal["stable"]
    ) -> StoredStableRecordingPipelineBatch | None: ...

    def get(
        self, id: UUID, *, mode: Literal["closed", "stable"] = "closed"
    ) -> StoredRecordingPipelineBatch | StoredStableRecordingPipelineBatch | None:
        row = (
            self.session.execute(
                select(
                    *UsageRecordingPipelineBatchRow.__table__.c,
                    UsageTraceRecordRow.fingerprint.label("source_fingerprint"),
                    UsageTraceRecordRow.source_count.label("source_count"),
                    UsageTraceRecordRow.after_seq.label("last_seq"),
                    UsageTraceRecordRow.created_at.label("source_created_at"),
                    UsageTraceAttestationRow.fingerprint.label("attestation_fingerprint"),
                    UsageTraceAttestationRow.record_id.label("attested_record_id"),
                    UsageResidencySegmentSetRow.attestation_id.label("segmented_attestation_id"),
                    UsageResidencySegmentSetRow.fingerprint.label("segment_set_fingerprint"),
                )
                .join(
                    UsageTraceRecordRow, UsageTraceRecordRow.id == UsageRecordingPipelineBatchRow.id
                )
                .join(
                    UsageTraceAttestationRow,
                    UsageTraceAttestationRow.id == UsageRecordingPipelineBatchRow.attestation_id,
                )
                .join(
                    UsageResidencySegmentSetRow,
                    UsageResidencySegmentSetRow.id == UsageRecordingPipelineBatchRow.segment_set_id,
                )
                .where(UsageRecordingPipelineBatchRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            # A broken FK must not look like an operation that never committed.
            if (
                self.session.scalar(
                    select(UsageRecordingPipelineBatchRow.id).where(
                        UsageRecordingPipelineBatchRow.id == str(id)
                    )
                )
                is not None
            ):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Pipeline batch dependency is missing", 503
                )
            return None
        try:
            raw = row["canonical_batch"]
            if len(raw.encode()) > MAX_PIPELINE_BATCH_BYTES:
                raise ValueError("oversized pipeline batch")
            decoded = json.loads(raw)
            model = (
                StoredStableRecordingPipelineBatch
                if mode == "stable"
                else StoredRecordingPipelineBatch
            )
            if (
                isinstance(decoded, dict)
                and decoded.get("assessment_scope")
                != model.model_fields["assessment_scope"].default
            ):
                raise DomainError(
                    "CONFLICT", "Pipeline batch belongs to another preparation mode", 409
                )
            value = model.model_validate(decoded)
            if (
                canonical_trace(value) != raw
                or (
                    str(value.request.source.id),
                    str(value.event_id),
                    value.configuration.trace.consumer_id,
                    value.revision,
                    value.source_record_fingerprint,
                    value.source_count,
                    value.last_seq,
                    str(value.attestation_id),
                    value.attestation_fingerprint,
                    str(value.segment_set_id),
                    value.segment_set_fingerprint,
                    value.created_at,
                    value.fingerprint,
                )
                != (
                    row["id"],
                    row["event_id"],
                    row["consumer_id"],
                    row["revision"],
                    row["source_fingerprint"],
                    row["source_count"],
                    row["last_seq"],
                    row["attestation_id"],
                    row["attestation_fingerprint"],
                    row["segment_set_id"],
                    row["segment_set_fingerprint"],
                    utc(row["created_at"]),
                    row["fingerprint"],
                )
                or value.source_cutoff < utc(row["source_created_at"])
                or row["attested_record_id"] != row["id"]
                or row["segmented_attestation_id"] != row["attestation_id"]
            ):
                raise ValueError("pipeline batch metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored pipeline batch is inconsistent", 503
            ) from None

    def append(
        self, value: StoredRecordingPipelineBatch | StoredStableRecordingPipelineBatch
    ) -> None:
        self.session.add(
            UsageRecordingPipelineBatchRow(
                id=str(value.request.source.id),
                event_id=str(value.event_id),
                consumer_id=value.configuration.trace.consumer_id,
                revision=value.revision,
                attestation_id=str(value.attestation_id),
                segment_set_id=str(value.segment_set_id),
                canonical_batch=canonical_trace(value),
                fingerprint=value.fingerprint,
                created_at=value.created_at,
            )
        )
