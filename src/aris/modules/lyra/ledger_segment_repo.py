"""Lyra-owned segment SQL; reads original immutable entries, never mutable claims."""

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import LargeBinary, and_, cast, exists, func, select
from sqlalchemy.orm import Session, aliased

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsageLedgerEntry, canonical_ledger
from aris.modules.lyra.ledger_segment_schemas import (
    MAX_LEDGER_SEGMENT_BYTES,
    MAX_LEDGER_SEGMENT_SOURCE_BYTES,
    StoredLedgerSegment,
)
from aris.modules.lyra.models import LedgerSegmentRow, UsageLedgerEntryRow


def segment_inconsistent() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored ledger segment is inconsistent", 503)


class LedgerSegmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id: UUID) -> StoredLedgerSegment | None:
        row = (
            self.session.execute(
                select(*LedgerSegmentRow.__table__.c).where(LedgerSegmentRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return self._stored(dict(row))

    @staticmethod
    def _stored(row: dict[str, Any]) -> StoredLedgerSegment:
        try:
            raw = row["canonical_segment"]
            if len(raw.encode()) > MAX_LEDGER_SEGMENT_BYTES:
                raise ValueError("oversized ledger segment")
            value = StoredLedgerSegment.model_validate_json(raw)
            if canonical_ledger(value) != raw or (
                str(value.request.id),
                str(value.event_id),
                str(value.request.previous.id) if value.request.previous else None,
                value.configuration.node_id,
                value.configuration.fingerprint,
                value.entries[0].revision,
                value.entries[-1].revision,
                str(value.entries[-1].entry_id),
                min(x.start_at for x in value.entries),
                max(x.end_at for x in value.entries),
                value.fingerprint,
                value.created_at,
            ) != (
                row["id"],
                row["event_id"],
                row["previous_id"],
                row["node_id"],
                row["configuration_fingerprint"],
                row["first_revision"],
                row["last_revision"],
                row["tail_entry_id"],
                utc(row["minimum_start"]),
                utc(row["maximum_end"]),
                row["fingerprint"],
                utc(row["created_at"]),
            ):
                raise ValueError("ledger segment metadata differs from its canonical record")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise segment_inconsistent() from None

    def latest(self, node_id: str) -> StoredLedgerSegment | None:
        id = self.session.scalar(
            select(LedgerSegmentRow.id)
            .where(LedgerSegmentRow.node_id == node_id)
            .order_by(LedgerSegmentRow.last_revision.desc())
            .limit(1)
        )
        return self.get(UUID(id)) if id is not None else None

    def latest_position(self, node_id: str) -> tuple[UUID, int, str] | None:
        """Scheduling metadata only; capture/read authority still verifies the canonical segment."""
        row = self.session.execute(
            select(
                LedgerSegmentRow.id,
                LedgerSegmentRow.last_revision,
                LedgerSegmentRow.configuration_fingerprint,
            )
            .where(LedgerSegmentRow.node_id == node_id)
            .order_by(LedgerSegmentRow.last_revision.desc())
            .limit(1)
        ).one_or_none()
        return (UUID(row[0]), row[1], row[2]) if row is not None else None

    def append(self, value: StoredLedgerSegment) -> None:
        self.session.add(
            LedgerSegmentRow(
                id=str(value.request.id),
                event_id=str(value.event_id),
                previous_id=str(value.request.previous.id) if value.request.previous else None,
                node_id=value.configuration.node_id,
                configuration_fingerprint=value.configuration.fingerprint,
                first_revision=value.entries[0].revision,
                last_revision=value.entries[-1].revision,
                tail_entry_id=str(value.entries[-1].entry_id),
                minimum_start=min(x.start_at for x in value.entries),
                maximum_end=max(x.end_at for x in value.entries),
                fingerprint=value.fingerprint,
                canonical_segment=canonical_ledger(value),
                created_at=value.created_at,
            )
        )

    def _size(self, scope: tuple[Any, ...]) -> tuple[int, int]:
        column = UsageLedgerEntryRow.canonical_entry
        size = (
            func.octet_length(column)
            if self.session.get_bind().dialect.name == "postgresql"
            else func.length(cast(column, LargeBinary))
        )
        count, encoded = self.session.execute(
            select(func.count(), func.coalesce(func.sum(size), 0)).where(*scope)
        ).one()
        return int(count), int(encoded)

    def entries(
        self, node_id: str, first: int, last: int
    ) -> tuple[tuple[UsageLedgerEntry, ...], int]:
        if not 0 < last - first + 1 <= 256:
            raise DomainError(
                "VALIDATION_ERROR", "Ledger segment range exceeds its entry budget", 422
            )
        scope = (
            UsageLedgerEntryRow.node_id == node_id,
            UsageLedgerEntryRow.revision.between(first, last),
        )
        count, size = self._size(scope)
        if count != last - first + 1:
            raise segment_inconsistent()
        if size > MAX_LEDGER_SEGMENT_SOURCE_BYTES:
            raise DomainError("LEDGER_SEGMENT_TOO_LARGE", "Split the original ledger segment", 503)
        rows = (
            self.session.execute(
                select(*UsageLedgerEntryRow.__table__.c)
                .where(*scope)
                .order_by(UsageLedgerEntryRow.revision)
                .limit(257)
            )
            .mappings()
            .all()
        )
        if len(rows) != count:
            raise segment_inconsistent()
        return tuple(UsageLedgerRepository._entry(dict(row)) for row in rows), size

    def targets(self, ids: tuple[UUID, ...], remaining_bytes: int) -> tuple[UsageLedgerEntry, ...]:
        if not ids:
            return ()
        if len(ids) > 256:
            raise segment_inconsistent()
        scope = (UsageLedgerEntryRow.id.in_(tuple(str(id) for id in ids)),)
        count, size = self._size(scope)
        if count != len(ids):
            raise segment_inconsistent()
        if size > remaining_bytes:
            raise DomainError(
                "LEDGER_SEGMENT_TOO_LARGE", "Split ledger segment target dependencies", 503
            )
        rows = (
            self.session.execute(select(*UsageLedgerEntryRow.__table__.c).where(*scope).limit(257))
            .mappings()
            .all()
        )
        if len(rows) != count:
            raise segment_inconsistent()
        return tuple(UsageLedgerRepository._entry(dict(row)) for row in rows)

    def has_historical_overlap(self, node_id: str, first: int, last: int) -> bool:
        incoming, old, reversal = (aliased(UsageLedgerEntryRow) for _ in range(3))
        already_reversed = exists(
            select(reversal.id).where(
                reversal.node_id == old.node_id,
                reversal.kind == "reversal",
                reversal.target_id == old.id,
                reversal.revision < incoming.revision,
            )
        ).correlate(incoming, old)
        query = (
            select(incoming.id)
            .select_from(incoming)
            .join(
                old,
                and_(
                    old.node_id == incoming.node_id,
                    old.kind == "measurement",
                    old.gpu_uuid == incoming.gpu_uuid,
                    old.revision < incoming.revision,
                    old.start_at < incoming.end_at,
                    old.end_at > incoming.start_at,
                ),
            )
            .where(
                incoming.node_id == node_id,
                incoming.kind == "measurement",
                incoming.revision.between(first, last),
                ~already_reversed,
            )
            .limit(1)
        )
        return self.session.scalar(query) is not None

    def covering(self, node_id: str, revision: int) -> StoredLedgerSegment | None:
        id = self.session.scalar(
            select(LedgerSegmentRow.id)
            .where(
                LedgerSegmentRow.node_id == node_id,
                LedgerSegmentRow.first_revision <= revision,
                LedgerSegmentRow.last_revision >= revision,
            )
            .order_by(LedgerSegmentRow.last_revision)
            .limit(1)
        )
        return self.get(UUID(id)) if id is not None else None

    def intersecting(
        self,
        node_id: str,
        through_revision: int,
        start_at: datetime,
        end_at: datetime,
        *,
        configuration_fingerprint: str,
        max_segments: int,
        max_bytes: int,
    ) -> Iterator[StoredLedgerSegment]:
        scope = (
            LedgerSegmentRow.node_id == node_id,
            LedgerSegmentRow.configuration_fingerprint == configuration_fingerprint,
            LedgerSegmentRow.first_revision <= through_revision,
            LedgerSegmentRow.minimum_start < end_at,
            LedgerSegmentRow.maximum_end > start_at,
        )
        column = LedgerSegmentRow.canonical_segment
        encoded = (
            func.octet_length(column)
            if self.session.get_bind().dialect.name == "postgresql"
            else func.length(cast(column, LargeBinary))
        )
        count, size = self.session.execute(
            select(func.count(), func.coalesce(func.sum(encoded), 0)).where(*scope)
        ).one()
        if int(count) > max_segments or int(size) > max_bytes:
            raise DomainError("USAGE_REPORT_TOO_LARGE", "Split the indexed ledger window", 503)
        seen, after = 0, 0
        while True:
            rows = (
                self.session.execute(
                    select(*LedgerSegmentRow.__table__.c)
                    .where(
                        *scope,
                        LedgerSegmentRow.first_revision > after,
                    )
                    .order_by(LedgerSegmentRow.first_revision)
                    .limit(16)
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            for row in rows:
                seen += 1
                if seen > max_segments:
                    raise segment_inconsistent()
                yield self._stored(dict(row))
            after = rows[-1]["first_revision"]
        if seen != int(count):
            raise segment_inconsistent()
