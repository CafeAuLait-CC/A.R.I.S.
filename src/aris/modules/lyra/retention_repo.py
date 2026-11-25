"""Read inventories of Lyra's durable dependencies without returning usage details."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.derivation_schemas import (
    MAX_DERIVATION_INPUT_BYTES,
    UsageDerivationRequest,
    canonical_derivation,
    derivation_fingerprint,
)
from aris.modules.lyra.models import (
    CoverageBuildPlanRow,
    CoverageBuildStepRow,
    UsageDerivation,
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

    def derivation_source_bounds(
        self, agent_id: str, node_id: str, *, maximum_records: int, maximum_characters: int
    ) -> tuple[int, int, int | None]:
        """Verify bounded original requests; source age never substitutes for references."""
        row = UsageDerivation
        selected = (row.agent_id == agent_id, row.node_id == node_id)
        count, characters = self.session.execute(
            select(
                func.count(), func.coalesce(func.sum(func.length(row.canonical_request)), 0)
            ).where(*selected)
        ).one()
        if count > maximum_records or characters > maximum_characters:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Derivation retention inventory exceeds its budget", 503
            )
        values = self.session.execute(
            select(
                row.id,
                row.source_count,
                row.identity_revision,
                row.as_of,
                row.request_fingerprint,
                row.canonical_request,
            )
            .where(*selected)
            .order_by(row.id)
            .limit(maximum_records + 1)
        )
        first = None
        reviewed = 0
        consumed = 0
        for record in values:
            reviewed += 1
            raw = record.canonical_request
            consumed += len(raw)
            if reviewed > count or consumed > maximum_characters:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Derivation inventory changed", 503)
            try:
                if len(raw.encode()) > MAX_DERIVATION_INPUT_BYTES:
                    raise ValueError("Oversized derivation request")
                request = UsageDerivationRequest.model_validate_json(raw)
                if (
                    canonical_derivation(request) != raw
                    or derivation_fingerprint(raw, result=False) != record.request_fingerprint
                    or str(request.id) != record.id
                    or len(request.sources) != record.source_count
                    or request.identity_revision != record.identity_revision
                    or request.as_of != utc(record.as_of)
                    or (request.sources[0].agent_id, request.sources[0].node_id)
                    != (agent_id, node_id)
                ):
                    raise ValueError("Derivation request metadata differs")
            except (ValueError, TypeError, RecursionError):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Derivation retention sources are inconsistent", 503
                ) from None
            candidate = request.sources[0].expected_seq
            first = candidate if first is None else min(first, candidate)
        if (reviewed, consumed) != (count, characters):
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Derivation inventory changed", 503)
        return reviewed, consumed, first

    def coverage_build_ids(self, node_id: str, limit: int) -> tuple[UUID, ...]:
        row = CoverageBuildPlanRow
        values = self.session.scalars(
            select(row.id).where(row.node_id == node_id).order_by(row.id).limit(limit)
        )
        try:
            return tuple(UUID(value) for value in values)
        except (ValueError, TypeError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Coverage build references are inconsistent", 503
            ) from None

    def saved_coverage_build_count(self, node_id: str, windows: tuple[UUID, ...]) -> int:
        row = UsageWindowSnapshotRow
        return self.session.execute(
            select(func.count())
            .select_from(row)
            .where(row.node_id == node_id, row.id.in_(tuple(str(id) for id in windows)))
        ).scalar_one()

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
