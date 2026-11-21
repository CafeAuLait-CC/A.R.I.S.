"""Read a fixed committed ledger window without loading the whole canonical history."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.ledger_schemas import UsageLedgerEntry, canonical_ledger
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository
from aris.modules.lyra.ledger_segment_schemas import (
    LedgerEntryIndex,
    LedgerSegmentConfiguration,
    LedgerSegmentReference,
    ledger_entry_index,
)
from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
from aris.modules.lyra.ledger_window_read import (
    MAX_LEDGER_WINDOW_ENTRIES,
    MAX_LEDGER_WINDOW_INDEX_BYTES,
    MAX_LEDGER_WINDOW_SEGMENT_BYTES,
    MAX_LEDGER_WINDOW_SEGMENTS,
    LedgerWindowQuery,
    LedgerWindowRead,
    window_read_fingerprint,
)


def _active_indexes(
    entries: tuple[LedgerEntryIndex, ...],
) -> tuple[tuple[LedgerEntryIndex, ...], tuple[LedgerEntryIndex, ...]]:
    by_id = {value.entry_id: value for value in entries}
    if len(by_id) != len(entries) or len({value.revision for value in entries}) != len(entries):
        raise inconsistent()
    reversals = tuple(value for value in entries if value.kind == "reversal")
    reversed_ids = set()
    for reversal in reversals:
        if reversal.target_id is None:
            raise inconsistent()
        original = by_id.get(reversal.target_id)
        if (
            original is None
            or original.kind != "measurement"
            or original.revision >= reversal.revision
            or original.fingerprint != reversal.target_fingerprint
            or original.window_fingerprint != reversal.window_fingerprint
            or original.entry_id in reversed_ids
        ):
            raise inconsistent()
        reversed_ids.add(original.entry_id)
    active = tuple(
        value
        for value in entries
        if value.kind == "measurement" and value.entry_id not in reversed_ids
    )
    previous: dict[str, LedgerEntryIndex] = {}
    for value in sorted(
        active, key=lambda item: (item.gpu_uuid, item.start_at, str(item.entry_id))
    ):
        old = previous.get(value.gpu_uuid)
        if old is not None and old.end_at > value.start_at:
            raise inconsistent()
        previous[value.gpu_uuid] = value
    return active, reversals


class LedgerWindowReader:
    """Internal data reader; the caller must enforce current own/all node permissions."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        configuration: LedgerSegmentConfiguration,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 10000,
    ) -> None:
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000:
            raise ValueError("ledger windows require a bounded read timeout")
        self.sessions = sessions
        self.configuration = LedgerSegmentConfiguration.model_validate(
            configuration.model_dump(mode="json")
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms
        self.segments = LedgerSegmentService(
            sessions, configuration=self.configuration, clock=self.clock
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger window clock requires timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _within(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Ledger window budget exceeded", 503)

    def read(
        self, query: LedgerWindowQuery, *, read_at: datetime | None = None
    ) -> LedgerWindowRead:
        try:
            with self.sessions() as db:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                elif db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN"))
                return self.read_in_transaction(db, query, read_at=read_at)
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Ledger window database is unavailable", 503
            ) from None

    def read_in_transaction(
        self,
        db: Session,
        query: LedgerWindowQuery,
        *,
        read_at: datetime | None = None,
        deadline: float | None = None,
    ) -> LedgerWindowRead:
        query = LedgerWindowQuery.model_validate(query.model_dump(mode="json"))
        now = self._now()
        at = read_at or now
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("ledger window data time requires timezone")
        at = at.astimezone(UTC)
        if query.node_id != self.configuration.node_id:
            raise DomainError("CONFLICT", "Ledger window node differs from its reader", 409)
        if query.end_at > at or at > now:
            raise DomainError("VALIDATION_ERROR", "Ledger window has not ended", 422)
        deadline = (
            min(deadline, monotonic() + self.timeout_ms / 1000)
            if deadline is not None
            else monotonic() + self.timeout_ms / 1000
        )
        self._within(deadline)
        remaining = min(self.timeout_ms, max(1, int((deadline - monotonic()) * 1000)))
        transaction_budget(db, min(100, remaining), min(1000, remaining), remaining)
        ledger = UsageLedgerRepository(db)
        repo = LedgerSegmentRepository(db)
        current = ledger.head(query.node_id)
        head = query.snapshot or current
        if head.revision > current.revision:
            raise DomainError("CONFLICT", "Ledger snapshot is not committed", 409)
        if head.revision:
            point = ledger.revision(head.node_id, head.revision)
            if point is None or point.fingerprint != head.fingerprint:
                raise DomainError("CONFLICT", "Ledger snapshot has different original content", 409)
            if read_at is None:
                now = self._now()
                at = now
            if point.recorded_at > at:
                raise DomainError("VALIDATION_ERROR", "Ledger snapshot exceeds its data time", 422)
        query = query.model_copy(update={"snapshot": head})
        latest = repo.latest(head.node_id)
        anchor = (
            latest
            if latest is not None and latest.request.target.revision <= head.revision
            else repo.covering(head.node_id, head.revision)
            if head.revision
            else None
        )
        if anchor is not None:
            if anchor.configuration != self.configuration:
                raise DomainError(
                    "CONFLICT", "Ledger index configuration or verification time differs", 409
                )
            if anchor.request.target.revision >= head.revision:
                offset = head.revision - anchor.entries[0].revision
                if (
                    not 0 <= offset < len(anchor.entries)
                    or anchor.entries[offset].fingerprint != head.fingerprint
                ):
                    raise inconsistent()
        before = anchor.request.target.revision if anchor else 0
        selected: list[LedgerEntryIndex] = []
        refs: list[LedgerSegmentReference] = []
        size = 0
        if anchor is not None:
            for segment in repo.intersecting(
                head.node_id,
                min(before, head.revision),
                query.start_at,
                query.end_at,
                configuration_fingerprint=self.configuration.fingerprint,
                max_segments=MAX_LEDGER_WINDOW_SEGMENTS,
                max_bytes=MAX_LEDGER_WINDOW_SEGMENT_BYTES,
            ):
                refs.append(segment.reference())
                for entry in segment.entries:
                    if entry.revision <= head.revision and self._matches(query, entry):
                        size = self._add(selected, entry, size, at)
                self._within(deadline)
        if head.revision > before:
            if head.revision - before > self.configuration.max_entries:
                raise DomainError("LEDGER_INDEX_BEHIND", "Ledger statistics are catching up", 503)
            original = self.segments.verify_tail_in_transaction(
                db,
                head,
                anchor.reference() if anchor else None,
                self.configuration,
                at=at,
                deadline=deadline,
            )
            for value in original:
                entry = ledger_entry_index(value)
                if self._matches(query, entry):
                    size = self._add(selected, entry, size, at)
        entries = tuple(selected)
        active, reversals = _active_indexes(entries)
        self._within(deadline)
        verified_at = self._now()
        if (
            verified_at < at
            or (anchor is not None and anchor.created_at > verified_at)
            or any(ref.created_at > verified_at for ref in refs)
        ):
            raise inconsistent()
        return LedgerWindowRead(
            query=query,
            head=head,
            read_at=at,
            verified_at=verified_at,
            anchor=anchor.reference() if anchor else None,
            segments=tuple(refs),
            entries=entries,
            active=active,
            reversals=reversals,
            fingerprint=window_read_fingerprint(query, entries),
        )

    @staticmethod
    def _matches(query: LedgerWindowQuery, entry: LedgerEntryIndex) -> bool:
        return (
            entry.start_at < query.end_at
            and entry.end_at > query.start_at
            and (query.owner_id is None or entry.owner_id == query.owner_id)
        )

    @staticmethod
    def _add(
        selected: list[LedgerEntryIndex], entry: LedgerEntryIndex, size: int, at: datetime
    ) -> int:
        if entry.recorded_at > at:
            raise inconsistent()
        selected.append(entry)
        size += len(canonical_ledger(entry).encode())
        if len(selected) > MAX_LEDGER_WINDOW_ENTRIES or size > MAX_LEDGER_WINDOW_INDEX_BYTES:
            raise DomainError(
                "USAGE_REPORT_TOO_LARGE", "Choose a shorter ledger statistics window", 503
            )
        return size

    def originals_in_transaction(
        self, db: Session, view: LedgerWindowRead, entries: tuple[LedgerEntryIndex, ...]
    ) -> tuple[UsageLedgerEntry, ...]:
        if view.head.node_id != self.configuration.node_id or len(entries) > 256:
            raise DomainError("VALIDATION_ERROR", "Invalid original ledger detail page", 422)
        members = {value.entry_id: value for value in view.entries}
        if len({value.entry_id for value in entries}) != len(entries) or any(
            members.get(value.entry_id) != value for value in entries
        ):
            raise DomainError(
                "CONFLICT", "Ledger detail page differs from its verified window", 409
            )
        found = LedgerSegmentRepository(db).targets(
            tuple(value.entry_id for value in entries), 8 * 1024 * 1024
        )
        by_id = {value.id: value for value in found}
        result = []
        for entry in entries:
            original = by_id.get(entry.entry_id)
            if (
                original is None
                or original.node_id != view.head.node_id
                or ledger_entry_index(
                    original,
                    include_posting_context=entry.posting_configuration_fingerprint is not None,
                )
                != entry
            ):
                raise inconsistent()
            result.append(original)
        return tuple(result)
