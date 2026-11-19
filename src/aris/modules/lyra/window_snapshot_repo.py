"""Lyra snapshot queries; all cross-domain evidence comes through public services."""

from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, LargeBinary, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageCoverageManifestRow, UsageWindowSnapshotRow
from aris.modules.lyra.trace_consumer_schemas import canonical_trace
from aris.modules.lyra.window_reader_schemas import SavedWindowSummary, WindowListQuery
from aris.modules.lyra.window_schemas import MAX_WINDOW_SOURCE_BYTES
from aris.modules.lyra.window_snapshot_schemas import (
    MAX_WINDOW_SNAPSHOT_BYTES,
    StoredUsageWindow,
    WindowManifestSelection,
)


class UsageWindowRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def summaries(self, query: WindowListQuery) -> tuple[SavedWindowSummary, ...]:
        row = UsageWindowSnapshotRow

        def field(name: str) -> ColumnElement[Any]:
            if self.session.get_bind().dialect.name == "postgresql":
                return func.jsonb_extract_path_text(
                    cast(row.canonical_snapshot, JSONB), "window", name
                )
            return func.json_extract(row.canonical_snapshot, "$.window." + name)

        clauses = [
            row.node_id == query.node_id,
            row.end_at > query.start_at,
            row.start_at < query.end_at,
            row.owner_id == str(query.owner_id)
            if query.owner_id is not None
            else row.owner_id.is_(None),
        ]
        if query.before is not None:
            clauses.append(
                or_(
                    row.created_at < query.before.created_at,
                    and_(row.created_at == query.before.created_at, row.id < str(query.before.id)),
                )
            )
        rows = self.session.execute(
            select(
                row.id,
                row.node_id,
                row.owner_id,
                row.start_at,
                row.end_at,
                row.created_at,
                field("coverage").label("coverage"),
                *(
                    cast(field(name), BigInteger).label(name)
                    for name in (
                        "recorded_microseconds",
                        "verified_usage_microseconds",
                        "total_usage_microseconds",
                    )
                ),
            )
            .where(*clauses)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(query.limit + 1)
        ).mappings()
        try:
            return tuple(
                SavedWindowSummary.model_validate(
                    {
                        **dict(value),
                        **{key: utc(value[key]) for key in ("start_at", "end_at", "created_at")},
                    }
                )
                for value in rows
            )
        except (ValueError, TypeError, KeyError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored window summaries are inconsistent", 503
            ) from None

    def scope(self, id: UUID) -> tuple[str, UUID | None] | None:
        row = self.session.execute(
            select(UsageWindowSnapshotRow.node_id, UsageWindowSnapshotRow.owner_id).where(
                UsageWindowSnapshotRow.id == str(id)
            )
        ).one_or_none()
        return (row[0], UUID(row[1]) if row[1] is not None else None) if row is not None else None

    def check_manifest_bytes(self, selections: tuple[WindowManifestSelection, ...]) -> None:
        if not selections:
            return
        column = UsageCoverageManifestRow.canonical_manifest
        length = (
            func.octet_length(column)
            if self.session.get_bind().dialect.name == "postgresql"
            else func.length(cast(column, LargeBinary))
        )
        count, size = self.session.execute(
            select(func.count(), func.coalesce(func.sum(length), 0)).where(
                UsageCoverageManifestRow.id.in_([str(item.id) for item in selections])
            )
        ).one()
        if count != len(selections):
            raise DomainError("NOT_FOUND", "Selected window manifest not found", 404)
        if size > MAX_WINDOW_SOURCE_BYTES:
            raise DomainError(
                "USAGE_REPORT_TOO_LARGE", "Window source manifests exceed the byte budget", 503
            )

    def get(self, id: UUID) -> StoredUsageWindow | None:
        row = (
            self.session.execute(
                select(*UsageWindowSnapshotRow.__table__.c).where(
                    UsageWindowSnapshotRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_snapshot"]
            if len(raw.encode()) > MAX_WINDOW_SNAPSHOT_BYTES:
                raise ValueError("oversized window snapshot")
            result = StoredUsageWindow.model_validate_json(raw)
            query = result.request.query
            if canonical_trace(result) != raw or (
                str(result.request.id),
                str(result.event_id),
                query.node_id,
                str(query.owner_id) if query.owner_id is not None else None,
                query.start_at,
                query.end_at,
                result.created_at,
                result.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["node_id"],
                row["owner_id"],
                utc(row["start_at"]),
                utc(row["end_at"]),
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("window snapshot metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored window snapshot is inconsistent", 503
            ) from None

    def append(self, value: StoredUsageWindow) -> None:
        query = value.request.query
        self.session.add(
            UsageWindowSnapshotRow(
                id=str(value.request.id),
                event_id=str(value.event_id),
                node_id=query.node_id,
                owner_id=str(query.owner_id) if query.owner_id is not None else None,
                start_at=query.start_at,
                end_at=query.end_at,
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_snapshot=canonical_trace(value),
            )
        )
