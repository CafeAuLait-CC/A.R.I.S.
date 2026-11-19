from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.ledger_schemas import canonical_ledger
from aris.modules.lyra.weekly_report_models import WeeklyReportRow
from aris.modules.lyra.weekly_report_schemas import (
    MAX_WEEKLY_REPORT_BYTES,
    StoredWeeklyReport,
    WeeklyReportCursor,
    WeeklyReportSummary,
)


class WeeklyReportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id: UUID) -> StoredWeeklyReport | None:
        row = (
            self.session.execute(
                select(*WeeklyReportRow.__table__.c).where(WeeklyReportRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_report"]
            if len(raw.encode()) > MAX_WEEKLY_REPORT_BYTES:
                raise ValueError("oversized report")
            value = StoredWeeklyReport.model_validate_json(raw)
            history = value.history
            if (
                canonical_ledger(value) != raw
                or value.content_fingerprint() != value.fingerprint
                or (
                    str(value.id),
                    str(value.event_id),
                    history.node_id,
                    str(history.owner_id) if history.owner_id is not None else None,
                    value.request.week,
                    history.timezone,
                    history.start_at,
                    history.end_at,
                    value.created_at,
                    history.recorded_microseconds,
                    history.snapshot.revision,
                    value.fingerprint,
                )
                != (
                    row["id"],
                    row["event_id"],
                    row["node_id"],
                    row["owner_id"],
                    row["week"],
                    row["timezone"],
                    utc(row["start_at"]),
                    utc(row["end_at"]),
                    utc(row["created_at"]),
                    row["recorded_microseconds"],
                    row["ledger_revision"],
                    row["fingerprint"],
                )
            ):
                raise ValueError("report metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored weekly report is inconsistent", 503
            ) from None

    def append(self, value: StoredWeeklyReport) -> None:
        history = value.history
        raw = canonical_ledger(value)
        if len(raw.encode()) > MAX_WEEKLY_REPORT_BYTES:
            raise DomainError(
                "USAGE_REPORT_TOO_LARGE", "Weekly report exceeds the byte budget", 503
            )
        self.session.add(
            WeeklyReportRow(
                id=str(value.id),
                event_id=str(value.event_id),
                node_id=history.node_id,
                owner_id=str(history.owner_id) if history.owner_id is not None else None,
                week=value.request.week,
                timezone=history.timezone,
                start_at=history.start_at,
                end_at=history.end_at,
                created_at=value.created_at,
                recorded_microseconds=history.recorded_microseconds,
                ledger_revision=history.snapshot.revision,
                fingerprint=value.fingerprint,
                canonical_report=raw,
            )
        )

    def summaries(
        self,
        *,
        node_id: str,
        owner_id: UUID | None,
        start_at: datetime,
        end_at: datetime,
        before: WeeklyReportCursor | None,
        limit: int,
    ) -> tuple[WeeklyReportSummary, ...]:
        row = WeeklyReportRow
        conditions = [
            row.node_id == node_id,
            row.end_at > start_at,
            row.start_at < end_at,
            row.owner_id == str(owner_id) if owner_id is not None else row.owner_id.is_(None),
        ]
        if before is not None:
            conditions.append(
                or_(
                    row.created_at < before.created_at,
                    and_(row.created_at == before.created_at, row.id < str(before.id)),
                )
            )
        columns = (
            row.id,
            row.node_id,
            row.owner_id,
            row.week,
            row.timezone,
            row.start_at,
            row.end_at,
            row.created_at,
            row.recorded_microseconds,
            row.ledger_revision,
        )
        values = self.session.execute(
            select(*columns)
            .where(*conditions)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(limit + 1)
        ).mappings()
        return tuple(
            WeeklyReportSummary.model_validate(
                {
                    **dict(value),
                    **{key: utc(value[key]) for key in ("start_at", "end_at", "created_at")},
                }
            )
            for value in values
        )
