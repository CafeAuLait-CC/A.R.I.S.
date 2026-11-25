from sqlalchemy import Insert, Update, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.lyra.reporting_gap_history_models import (
    ReportingGapHistoryRow,
    ReportingGapScanRow,
    ReportingPrefixScanRow,
)
from aris.modules.lyra.reporting_gap_history_schemas import (
    MAX_GAP_RECORD_CHARACTERS,
    ReportingGapScanPosition,
    ReportingPrefixPosition,
    StoredReportingGap,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


class ReportingGapHistoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prefix_position(self, source: UsageArchiveHeadQuery) -> ReportingPrefixPosition | None:
        row = self.session.get(ReportingPrefixScanRow, (source.node_id, source.agent_id))
        if row is None:
            return None
        try:
            raw = row.canonical_position
            if len(raw) > MAX_GAP_RECORD_CHARACTERS:
                raise ValueError("Oversized historical reporting prefix")
            value = ReportingPrefixPosition.model_validate_json(raw)
            if (
                value.source != source
                or value.target_through_seq != row.target_through_seq
                or int(value.last_report.seq) != row.last_seq
                or canonical_trace(value) != raw
            ):
                raise ValueError("Historical prefix metadata differs")
            return value
        except (ValueError, TypeError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Historical reporting prefix is inconsistent", 503
            ) from None

    def advance_prefix(
        self, position: ReportingPrefixPosition, previous: ReportingPrefixPosition | None
    ) -> None:
        row, source = ReportingPrefixScanRow, position.source
        if previous is not None and (
            position.source != previous.source
            or position.target_through_seq != previous.target_through_seq
            or int(position.last_report.seq) <= int(previous.last_report.seq)
        ):
            raise DomainError("CONFLICT", "Historical reporting prefix cannot change range", 409)
        raw = canonical_trace(position)
        values = {
            "last_seq": int(position.last_report.seq),
            "target_through_seq": position.target_through_seq,
            "canonical_position": raw,
        }
        if len(raw) > MAX_GAP_RECORD_CHARACTERS:
            raise DomainError("VALIDATION_ERROR", "Historical prefix exceeds its size limit", 422)
        statement: Insert | Update
        if previous is None:
            insert = (
                sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
            )
            statement = (
                insert(row)
                .values(node_id=source.node_id, agent_id=source.agent_id, **values)
                .on_conflict_do_nothing(index_elements=[row.node_id, row.agent_id])
            )
        else:
            statement = (
                update(row)
                .where(
                    row.node_id == source.node_id,
                    row.agent_id == source.agent_id,
                    row.last_seq == int(previous.last_report.seq),
                    row.target_through_seq == previous.target_through_seq,
                    row.canonical_position == canonical_trace(previous),
                )
                .values(**values)
            )
        if self.session.scalar(statement.returning(row.last_seq)) != values["last_seq"]:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Historical reporting prefix changed; retry", 503
            )

    def latest(
        self, source: UsageArchiveHeadQuery, limit: int, before_seq: int | None
    ) -> tuple[StoredReportingGap, ...]:
        row = ReportingGapHistoryRow
        query = select(row).where(row.node_id == source.node_id, row.agent_id == source.agent_id)
        if before_seq is not None:
            query = query.where(row.next_seq < before_seq)
        return tuple(
            self._read(value, source)
            for value in self.session.scalars(query.order_by(row.next_seq.desc()).limit(limit))
        )

    def save(
        self, source: UsageArchiveHeadQuery, observations: tuple[StoredReportingGap, ...]
    ) -> int:
        row = ReportingGapHistoryRow
        values = {int(value.gap.next_report.seq): value for value in observations}
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        inserted = tuple(
            self.session.scalars(
                insert(row)
                .values(
                    [
                        {
                            "node_id": source.node_id,
                            "agent_id": source.agent_id,
                            "next_seq": seq,
                            "canonical_record": canonical_trace(observation),
                        }
                        for seq, observation in values.items()
                    ]
                )
                .on_conflict_do_nothing(index_elements=[row.node_id, row.agent_id, row.next_seq])
                .returning(row.next_seq)
            )
        )
        stored = tuple(
            self.session.scalars(
                select(row).where(
                    row.node_id == source.node_id,
                    row.agent_id == source.agent_id,
                    row.next_seq.in_(values),
                )
            )
        )
        if len(stored) != len(values):
            raise DomainError("CONFLICT", "Reporting gap capture lost a stored observation", 409)
        for record in stored:
            found = self._read(record, source)
            expected = values[record.next_seq]
            if (found.gap, found.threshold_seconds) != (expected.gap, expected.threshold_seconds):
                raise DomainError("CONFLICT", "Stored reporting gap has different evidence", 409)
        return len(inserted)

    def position(self, source: UsageArchiveHeadQuery) -> ReportingGapScanPosition | None:
        row = self.session.get(ReportingGapScanRow, (source.node_id, source.agent_id))
        if row is None:
            return None
        try:
            raw = row.canonical_position
            if len(raw) > MAX_GAP_RECORD_CHARACTERS:
                raise ValueError("Oversized reporting scan position")
            value = ReportingGapScanPosition.model_validate_json(raw)
            if (
                value.source != source
                or int(value.last_report.seq) != row.through_seq
                or canonical_trace(value) != raw
            ):
                raise ValueError("Reporting scan position metadata differs")
            return value
        except (ValueError, TypeError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Reporting scan position is inconsistent", 503
            ) from None

    def advance(
        self,
        position: ReportingGapScanPosition,
        previous: ReportingGapScanPosition | None,
    ) -> None:
        row, source = ReportingGapScanRow, position.source
        value = {
            "through_seq": int(position.last_report.seq),
            "canonical_position": canonical_trace(position),
        }
        statement: Insert | Update
        if previous is None:
            insert = (
                sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
            )
            statement = (
                insert(row)
                .values(node_id=source.node_id, agent_id=source.agent_id, **value)
                .on_conflict_do_nothing(index_elements=[row.node_id, row.agent_id])
            )
        else:
            statement = (
                update(row)
                .where(
                    row.node_id == source.node_id,
                    row.agent_id == source.agent_id,
                    row.through_seq == int(previous.last_report.seq),
                    row.canonical_position == canonical_trace(previous),
                )
                .values(**value)
            )
        if self.session.scalar(statement.returning(row.through_seq)) != value["through_seq"]:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Reporting scan position changed; retry", 503
            )

    @staticmethod
    def _read(row: ReportingGapHistoryRow, source: UsageArchiveHeadQuery) -> StoredReportingGap:
        try:
            raw = row.canonical_record
            if len(raw) > MAX_GAP_RECORD_CHARACTERS:
                raise ValueError("Oversized reporting gap evidence")
            value = StoredReportingGap.model_validate_json(raw)
            if (
                value.source != source
                or int(value.gap.next_report.seq) != row.next_seq
                or canonical_trace(value) != raw
            ):
                raise ValueError("Reporting gap metadata differs from its evidence")
            return value
        except (ValueError, TypeError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored reporting gap evidence is inconsistent", 503
            ) from None
