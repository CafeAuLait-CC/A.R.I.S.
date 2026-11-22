import hashlib
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.coverage_schedule_models import (
    CoveragePeriodResultRow,
    CoverageScheduleRow,
    DailyCoverageEvidenceCheckRow,
    DailyCoveragePeriodResultRow,
    VerifiedDailyCoverageRevisionRow,
)
from aris.modules.nova.coverage_schedule_schemas import (
    CoveragePeriodResult,
    CoverageScheduleDefinition,
    CoverageScheduleStatus,
    DailyCoverageEvidenceCheck,
    DailyCoveragePeriodResult,
    VerifiedDailyCoverageRevision,
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
                prior = self.last_result(value)
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

    def last_result(
        self, status: CoverageScheduleStatus
    ) -> CoveragePeriodResult | DailyCoveragePeriodResult | None:
        id = status.last_result_id
        if id is None:
            return None
        return self.daily_result(id) if status.definition.daily_rollups else self.result(id)

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

    def daily_result(self, id: UUID) -> DailyCoveragePeriodResult | None:
        row = self.session.get(DailyCoveragePeriodResultRow, str(id))
        if row is None:
            return None
        try:
            raw = row.canonical_result
            if len(raw.encode()) > 262144:
                raise ValueError("oversized result")
            value = DailyCoveragePeriodResult.model_validate_json(raw)
            if schedule_canonical(value) != raw or (
                str(value.id),
                str(value.schedule_id),
                value.week,
                str(value.event_id),
                value.fingerprint,
            ) != (row.id, row.schedule_id, row.week, row.event_id, row.fingerprint):
                raise ValueError("daily report metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def evidence_checks(self, report_id: UUID) -> tuple[DailyCoverageEvidenceCheck, ...]:
        row = DailyCoverageEvidenceCheckRow
        rows = self.session.scalars(
            select(row).where(row.report_id == str(report_id)).order_by(row.day).limit(8)
        )
        try:
            return tuple(
                DailyCoverageEvidenceCheck(
                    report_id=report_id,
                    day=value.day,
                    report_fingerprint=value.report_fingerprint,
                    snapshot_fingerprint=value.snapshot_fingerprint,
                    rollup_fingerprint=value.rollup_fingerprint,
                    checked_at=utc(value.checked_at),
                )
                for value in rows
            )
        except (ValueError, TypeError):
            raise inconsistent() from None

    def add_evidence_check(self, value: DailyCoverageEvidenceCheck) -> None:
        row = DailyCoverageEvidenceCheckRow
        insert = (
            postgres_insert
            if self.session.get_bind().dialect.name == "postgresql"
            else sqlite_insert
        )
        self.session.execute(
            insert(row)
            .values(
                **value.model_dump(exclude={"report_id"}),
                report_id=str(value.report_id),
            )
            .on_conflict_do_nothing(index_elements=[row.report_id, row.day])
        )

    def verified_revision(
        self, report_id: UUID, revision_id: UUID
    ) -> VerifiedDailyCoverageRevision | None:
        row = self.session.get(VerifiedDailyCoverageRevisionRow, (str(report_id), str(revision_id)))
        if row is None:
            return None
        try:
            raw = row.canonical_revision
            if len(raw.encode()) > 262144:
                raise ValueError("oversized revision")
            result = VerifiedDailyCoverageRevision.model_validate_json(raw)
            if schedule_canonical(result) != raw or (
                result.original_report_id,
                result.revision_id,
                result.fingerprint,
            ) != (report_id, revision_id, row.fingerprint):
                raise ValueError("verified revision metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def add_verified_revision(self, value: VerifiedDailyCoverageRevision) -> None:
        row = VerifiedDailyCoverageRevisionRow
        raw = schedule_canonical(value)
        if len(raw.encode()) > 262144:
            raise DomainError("VALIDATION_ERROR", "Verified revision exceeds its size limit", 422)
        insert = (
            postgres_insert
            if self.session.get_bind().dialect.name == "postgresql"
            else sqlite_insert
        )
        self.session.execute(
            insert(row)
            .values(
                original_report_id=str(value.original_report_id),
                revision_id=str(value.revision_id),
                fingerprint=value.fingerprint,
                canonical_revision=raw,
            )
            .on_conflict_do_nothing(index_elements=[row.original_report_id, row.revision_id])
        )

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
        self._advance(status, result.id, result.week, result.created_at)

    def complete_daily(
        self, status: CoverageScheduleStatus, result: DailyCoveragePeriodResult
    ) -> None:
        raw = schedule_canonical(result)
        if len(raw.encode()) > 262144:
            raise DomainError("USAGE_REPORT_TOO_LARGE", "Weekly coverage result is too large", 503)
        self.session.add(
            DailyCoveragePeriodResultRow(
                id=str(result.id),
                schedule_id=str(result.schedule_id),
                week=result.week,
                event_id=str(result.event_id),
                fingerprint=result.fingerprint,
                canonical_result=raw,
            )
        )
        self._advance(status, result.id, result.week, result.created_at)

    def _advance(self, status: CoverageScheduleStatus, id: UUID, week: date, at: datetime) -> None:
        self.session.execute(
            update(CoverageScheduleRow)
            .where(CoverageScheduleRow.id == str(status.definition.id))
            .values(
                next_week=week + timedelta(days=7),
                revision=status.revision + 1,
                updated_at=at,
                last_result_id=str(id),
            )
        )
