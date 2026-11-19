"""Lyra snapshot queries; all cross-domain evidence comes through public services."""

from uuid import UUID

from sqlalchemy import LargeBinary, cast, func, select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import UsageCoverageManifestRow, UsageWindowSnapshotRow
from aris.modules.lyra.trace_consumer_schemas import canonical_trace
from aris.modules.lyra.window_schemas import MAX_WINDOW_SOURCE_BYTES
from aris.modules.lyra.window_snapshot_schemas import (
    MAX_WINDOW_SNAPSHOT_BYTES,
    StoredUsageWindow,
    WindowManifestSelection,
)


class UsageWindowRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

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
