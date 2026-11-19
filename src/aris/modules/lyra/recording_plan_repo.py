"""Lyra-owned immutable plan metadata and transaction-locked dispatch progress."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageRecordingPlanRow
from aris.modules.lyra.recording_plan_schemas import (
    MAX_RECORDING_PLAN_BYTES,
    RecordingPlanProgress,
    StoredRecordingPlan,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class RecordingPlanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_operation(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.lyra.recording_plan.lock\x00" + id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    @staticmethod
    def _read(row: dict[str, Any]) -> RecordingPlanProgress:
        try:
            raw = row["canonical_plan"]
            if len(raw.encode()) > MAX_RECORDING_PLAN_BYTES:
                raise ValueError("oversized recording plan")
            plan = StoredRecordingPlan.model_validate(json.loads(raw))
            if canonical_trace(plan) != raw or (
                str(plan.request.id),
                str(plan.event_id),
                plan.consumer_id,
                plan.parent_run.id,
                str(plan.request.partition.reference.record_id),
                str(plan.request.partition.closing_attestation_id),
                plan.partition_count,
                plan.created_at,
                plan.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["consumer_id"],
                row["run_id"],
                row["source_record_id"],
                row["closing_attestation_id"],
                row["partition_count"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("recording plan metadata differs")
            return RecordingPlanProgress(
                plan=plan,
                version=row["version"],
                next_partition=row["next_partition"],
                updated_at=utc(row["updated_at"]),
                all_enqueued=row["next_partition"] == plan.partition_count,
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored recording plan is inconsistent", 503
            ) from None

    def get(self, id: UUID) -> RecordingPlanProgress | None:
        row = (
            self.session.execute(
                select(*UsageRecordingPlanRow.__table__.c).where(
                    UsageRecordingPlanRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._read(dict(row)) if row is not None else None

    def locked(self, id: UUID) -> UsageRecordingPlanRow | None:
        return self.session.scalar(
            select(UsageRecordingPlanRow)
            .where(UsageRecordingPlanRow.id == str(id))
            .with_for_update()
        )

    def append(self, plan: StoredRecordingPlan) -> None:
        self.session.add(
            UsageRecordingPlanRow(
                id=str(plan.request.id),
                event_id=str(plan.event_id),
                consumer_id=plan.consumer_id,
                run_id=plan.parent_run.id,
                source_record_id=str(plan.request.partition.reference.record_id),
                closing_attestation_id=str(plan.request.partition.closing_attestation_id),
                partition_count=plan.partition_count,
                canonical_plan=canonical_trace(plan),
                created_at=plan.created_at,
                fingerprint=plan.fingerprint,
                version=1,
                next_partition=0,
                updated_at=plan.created_at,
            )
        )
