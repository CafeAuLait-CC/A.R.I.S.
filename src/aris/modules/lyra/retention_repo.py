"""Read inventories of Lyra's durable dependencies without returning usage details."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.modules.lyra.models import (
    CoverageBuildPlanRow,
    CoverageBuildStepRow,
    UsageRecordingJobRow,
    UsageTraceAttestationRow,
    UsageTraceCursorRow,
    UsageTraceRecordRow,
    UsageWindowSnapshotRow,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository, inconsistent_trace
from aris.modules.lyra.trace_consumer_schemas import UsageTracePosition
from aris.modules.lyra.weekly_report_models import WeeklyReportRow


class RetentionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def cursor_count(self) -> int:
        cursor, history = UsageTraceCursorRow, UsageTraceRecordRow
        detached = self.session.scalar(
            select(history.consumer_id)
            .where(
                ~select(cursor.consumer_id)
                .where(cursor.consumer_id == history.consumer_id)
                .exists()
            )
            .limit(1)
        )
        if detached is not None:
            raise inconsistent_trace()
        return self.session.execute(select(func.count()).select_from(cursor)).scalar_one()

    def positions(self, limit: int) -> tuple[UsageTracePosition, ...]:
        cursor, history = UsageTraceCursorRow, UsageTraceRecordRow
        latest = (
            select(func.max(history.revision))
            .where(history.consumer_id == cursor.consumer_id)
            .correlate(cursor)
            .scalar_subquery()
        )
        rows = self.session.execute(
            select(
                cursor.consumer_id,
                cursor.record_id,
                cursor.revision,
                cursor.after_seq,
                cursor.fingerprint,
                latest.label("latest_revision"),
            )
            .order_by(cursor.consumer_id)
            .limit(limit)
        ).all()
        traces = UsageTraceRepository(self.session)
        positions = []
        for row in rows:
            if row.latest_revision != row.revision:
                raise inconsistent_trace()
            record = traces.get(UUID(row.record_id))
            if record is None:
                raise inconsistent_trace()
            position = record.position
            if (
                position.configuration.consumer_id,
                position.revision,
                position.after_seq,
                position.fingerprint,
            ) != (row.consumer_id, row.revision, row.after_seq, row.fingerprint):
                raise inconsistent_trace()
            positions.append(position)
        return tuple(positions)

    def jobs_by_state(self) -> dict[str, int]:
        row = UsageRecordingJobRow
        return {
            state: count
            for state, count in self.session.execute(
                select(row.state, func.count()).group_by(row.state).order_by(row.state)
            )
        }

    def evidence_counts(self) -> dict[str, int]:
        tables = {
            "source_attestations": UsageTraceAttestationRow,
            "coverage_build_plans": CoverageBuildPlanRow,
            "coverage_build_steps": CoverageBuildStepRow,
            "saved_windows": UsageWindowSnapshotRow,
            "saved_recorded_weekly_reports": WeeklyReportRow,
        }
        row = (
            self.session.execute(
                select(
                    *(
                        select(func.count()).select_from(table).scalar_subquery().label(name)
                        for name, table in tables.items()
                    )
                )
            )
            .mappings()
            .one()
        )
        return {name: int(row[name]) for name in tables}
