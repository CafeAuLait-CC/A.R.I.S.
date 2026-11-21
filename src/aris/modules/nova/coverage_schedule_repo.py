import hashlib
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.coverage_schedule_models import CoveragePeriodResultRow, CoverageScheduleRow
from aris.modules.nova.coverage_schedule_schemas import (
    CoveragePeriodResult,
    CoverageScheduleDefinition,
    CoverageScheduleStatus,
    schedule_canonical,
)


def inconsistent() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored coverage schedule is inconsistent", 503)


class CoverageScheduleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.nova.coverage-schedule-lock\x00" + id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def get(self, id: UUID, *, lock: bool = False) -> CoverageScheduleStatus | None:
        statement = select(*CoverageScheduleRow.__table__.c).where(
            CoverageScheduleRow.id == str(id)
        )
        row = (
            self.session.execute(statement.with_for_update() if lock else statement)
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_definition"]
            if len(raw.encode()) > 16384:
                raise ValueError("oversized definition")
            definition = CoverageScheduleDefinition.model_validate_json(raw)
            if schedule_canonical(definition) != raw or (
                str(definition.id),
                definition.fingerprint,
                definition.calendar.first_week,
            ) != (row["id"], row["fingerprint"], row["first_week"]):
                raise ValueError("definition metadata differs")
            value = CoverageScheduleStatus(
                definition=definition,
                next_week=row["next_week"],
                revision=row["revision"],
                created_at=utc(row["created_at"]),
                updated_at=utc(row["updated_at"]),
                last_result_id=row["last_result_id"],
            )
            if value.last_result_id is not None:
                prior = self.result(value.last_result_id)
                if (
                    prior is None
                    or prior.schedule_id != definition.id
                    or prior.definition_fingerprint != definition.fingerprint
                    or prior.week + timedelta(days=7) != value.next_week
                ):
                    raise ValueError("last result differs from cursor")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def result(self, id: UUID) -> CoveragePeriodResult | None:
        row = (
            self.session.execute(
                select(*CoveragePeriodResultRow.__table__.c).where(
                    CoveragePeriodResultRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_result"]
            if len(raw.encode()) > 16384:
                raise ValueError("oversized result")
            result = CoveragePeriodResult.model_validate_json(raw)
            if schedule_canonical(result) != raw or (
                str(result.id),
                str(result.schedule_id),
                result.week,
                str(result.snapshot_id),
                str(result.event_id),
                result.fingerprint,
            ) != (
                row["id"],
                row["schedule_id"],
                row["week"],
                row["snapshot_id"],
                row["event_id"],
                row["fingerprint"],
            ):
                raise ValueError("result metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def append(self, value: CoverageScheduleStatus) -> None:
        self.session.add(
            CoverageScheduleRow(
                id=str(value.definition.id),
                canonical_definition=schedule_canonical(value.definition),
                fingerprint=value.definition.fingerprint,
                first_week=value.definition.calendar.first_week,
                next_week=value.next_week,
                revision=value.revision,
                created_at=value.created_at,
                updated_at=value.updated_at,
                last_result_id=None,
            )
        )

    def complete(self, status: CoverageScheduleStatus, result: CoveragePeriodResult) -> None:
        self.session.add(
            CoveragePeriodResultRow(
                id=str(result.id),
                schedule_id=str(result.schedule_id),
                week=result.week,
                snapshot_id=str(result.snapshot_id),
                event_id=str(result.event_id),
                fingerprint=result.fingerprint,
                canonical_result=schedule_canonical(result),
            )
        )
        self.session.execute(
            update(CoverageScheduleRow)
            .where(CoverageScheduleRow.id == str(status.definition.id))
            .values(
                next_week=result.week + timedelta(days=7),
                revision=status.revision + 1,
                updated_at=result.created_at,
                last_result_id=str(result.id),
            )
        )
