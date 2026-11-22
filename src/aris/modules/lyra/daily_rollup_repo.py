"""Bounded reads and idempotent inserts of immutable daily usage rollups."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_rollup import SavedCoverageRollup
from aris.modules.lyra.daily_rollup_models import DailyUsageRollupRow
from aris.modules.lyra.trace_consumer_schemas import canonical_trace

MAX_DAILY_ROLLUP_BYTES = 262144


class DailyUsageRollupRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def fingerprints(self, node_id: str, ids: tuple[UUID, ...]) -> dict[UUID, str]:
        row = DailyUsageRollupRow
        matches = self.db.execute(
            select(row.snapshot_id, row.snapshot_fingerprint).where(
                row.node_id == node_id, row.snapshot_id.in_([str(id) for id in ids])
            )
        )
        return {UUID(id): fingerprint for id, fingerprint in matches}

    def get(self, node_id: str, ids: tuple[UUID, ...]) -> dict[UUID, SavedCoverageRollup]:
        row = DailyUsageRollupRow
        matches = self.db.scalars(
            select(row).where(row.node_id == node_id, row.snapshot_id.in_([str(id) for id in ids]))
        )
        result = {}
        try:
            for match in matches:
                raw = match.canonical_rollup
                if len(raw.encode()) > MAX_DAILY_ROLLUP_BYTES:
                    raise ValueError("Daily rollup is too large")
                value = SavedCoverageRollup.model_validate_json(raw)
                if (
                    str(value.snapshot_id) != match.snapshot_id
                    or value.query.node_id != node_id
                    or value.snapshot_fingerprint != match.snapshot_fingerprint
                    or value.fingerprint != match.fingerprint
                    or value.fingerprint != value.content_fingerprint()
                    or canonical_trace(value) != raw
                ):
                    raise ValueError("Daily rollup fingerprint or source differs")
                result[value.snapshot_id] = value
        except (ValueError, TypeError):
            raise DomainError(
                "CONFLICT", "Stored daily usage rollup is inconsistent", 409
            ) from None
        return result

    def insert(self, value: SavedCoverageRollup) -> None:
        raw = canonical_trace(value)
        if len(raw.encode()) > MAX_DAILY_ROLLUP_BYTES:
            raise DomainError("VALIDATION_ERROR", "Daily usage rollup exceeds its size limit", 422)
        row = DailyUsageRollupRow
        insert = (
            postgres_insert if self.db.get_bind().dialect.name == "postgresql" else sqlite_insert
        )
        added = self.db.scalar(
            insert(row)
            .values(
                snapshot_id=str(value.snapshot_id),
                node_id=value.query.node_id,
                snapshot_fingerprint=value.snapshot_fingerprint,
                fingerprint=value.fingerprint,
                canonical_rollup=raw,
            )
            .on_conflict_do_nothing(index_elements=[row.snapshot_id])
            .returning(row.snapshot_id)
        )
        if (
            added is None
            and self.get(value.query.node_id, (value.snapshot_id,)).get(value.snapshot_id) != value
        ):
            raise DomainError("CONFLICT", "Daily usage rollup has different fixed input", 409)
