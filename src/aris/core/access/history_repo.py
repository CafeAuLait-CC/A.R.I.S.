from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.access.history_schemas import (
    MAX_MAPPING_HISTORY_CHANGES,
    HistoricalMappingValue,
    HistoricalNodeUserQuery,
    MappingHistoryEntry,
    mapping_history_fingerprint,
)
from aris.core.access.models import AccessDirectoryState, NodeUserMapping, NodeUserMappingHistory
from aris.core.contracts import DomainError
from aris.core.time import utc


class MappingHistoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def revision(self) -> int | None:
        return self.session.scalar(
            select(AccessDirectoryState.revision).where(
                AccessDirectoryState.id == "primary", AccessDirectoryState.completed_at.is_not(None)
            )
        )

    def current(self, node_id: str, username: str) -> HistoricalMappingValue | None:
        row = self.session.execute(
            select(
                NodeUserMapping.user_id, NodeUserMapping.confidence, NodeUserMapping.source
            ).where(NodeUserMapping.node_id == node_id, NodeUserMapping.os_username == username)
        ).one_or_none()
        if row is None:
            return None
        try:
            return HistoricalMappingValue(user_id=row[0], confidence=row[1], source=row[2])
        except ValueError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Current mapping metadata is invalid", 503
            ) from None

    @staticmethod
    def _entry(row: dict[str, Any]) -> MappingHistoryEntry:
        try:

            def value(prefix: str) -> HistoricalMappingValue | None:
                if row[prefix + "_user_id"] is None:
                    if (
                        row[prefix + "_confidence"] is not None
                        or row[prefix + "_source"] is not None
                    ):
                        raise ValueError("absent mapping has unexpected metadata")
                    return None
                return HistoricalMappingValue(
                    user_id=row[prefix + "_user_id"],
                    confidence=row[prefix + "_confidence"],
                    source=row[prefix + "_source"],
                )

            entry = MappingHistoryEntry(
                id=row["id"],
                node_id=row["node_id"],
                os_username=row["os_username"],
                revision=row["revision"],
                effective_at=utc(row["effective_at"]),
                event_id=row["event_id"],
                actor_id=row["actor_id"],
                operation_id=row["operation_id"],
                request_id=row["request_id"],
                reason=row["reason"],
                before=value("before"),
                after=value("after"),
                fingerprint=row["fingerprint"],
            )
            if mapping_history_fingerprint(entry) != entry.fingerprint:
                raise ValueError("mapping history fingerprint differs")
            return entry
        except (ValueError, TypeError, KeyError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Mapping history is inconsistent", 503
            ) from None

    def latest(self, node_id: str, username: str) -> MappingHistoryEntry | None:
        row = (
            self.session.execute(
                select(*NodeUserMappingHistory.__table__.c)
                .where(
                    NodeUserMappingHistory.node_id == node_id,
                    NodeUserMappingHistory.os_username == username,
                )
                .order_by(NodeUserMappingHistory.revision.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return self._entry(dict(row)) if row is not None else None

    def covering(
        self, query: HistoricalNodeUserQuery, through_revision: int
    ) -> MappingHistoryEntry | None:
        row = (
            self.session.execute(
                select(*NodeUserMappingHistory.__table__.c)
                .where(
                    NodeUserMappingHistory.node_id == query.node_id,
                    NodeUserMappingHistory.os_username == query.os_username,
                    NodeUserMappingHistory.effective_at <= query.start_at,
                    NodeUserMappingHistory.effective_at <= query.as_of,
                    NodeUserMappingHistory.revision <= through_revision,
                )
                .order_by(
                    NodeUserMappingHistory.effective_at.desc(),
                    NodeUserMappingHistory.revision.desc(),
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return self._entry(dict(row)) if row is not None else None

    def changes(
        self, query: HistoricalNodeUserQuery, through_revision: int
    ) -> tuple[MappingHistoryEntry, ...]:
        rows = self.session.execute(
            select(*NodeUserMappingHistory.__table__.c)
            .where(
                NodeUserMappingHistory.node_id == query.node_id,
                NodeUserMappingHistory.os_username == query.os_username,
                NodeUserMappingHistory.effective_at > query.start_at,
                NodeUserMappingHistory.effective_at < query.end_at,
                NodeUserMappingHistory.effective_at <= query.as_of,
                NodeUserMappingHistory.revision <= through_revision,
            )
            .order_by(NodeUserMappingHistory.effective_at, NodeUserMappingHistory.revision)
            .limit(MAX_MAPPING_HISTORY_CHANGES + 1)
        ).mappings()
        result = tuple(self._entry(dict(row)) for row in rows)
        if len(result) > MAX_MAPPING_HISTORY_CHANGES:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Mapping history window exceeds its budget", 503
            )
        return result

    def append(self, entry: MappingHistoryEntry) -> None:
        values = {
            key: str(getattr(entry, key))
            for key in (
                "id",
                "event_id",
                "actor_id",
                "operation_id",
                "request_id",
                "reason",
                "node_id",
                "os_username",
                "fingerprint",
            )
        }
        fields: dict[str, Any] = {
            **values,
            "revision": entry.revision,
            "effective_at": entry.effective_at,
        }
        for prefix, item in (("before", entry.before), ("after", entry.after)):
            fields.update(
                {
                    prefix + "_user_id": str(item.user_id) if item else None,
                    prefix + "_confidence": item.confidence if item else None,
                    prefix + "_source": item.source if item else None,
                }
            )
        self.session.add(NodeUserMappingHistory(**fields))
