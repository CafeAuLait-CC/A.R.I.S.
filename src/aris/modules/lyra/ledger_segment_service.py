"""Create bounded original-chain verification segments, without recording usage or deleting it."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_plan import MeasuredWindow
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    ReversedMeasurement,
    UsageLedgerEntry,
    UsageLedgerHead,
    canonical_ledger,
)
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository, segment_inconsistent
from aris.modules.lyra.ledger_segment_schemas import (
    MAX_LEDGER_SEGMENT_BYTES,
    MAX_LEDGER_SEGMENT_SOURCE_BYTES,
    CaptureLedgerSegment,
    LedgerRangeBoundary,
    LedgerSegmentConfiguration,
    LedgerSegmentReference,
    StoredLedgerSegment,
    ledger_entry_index,
    ledger_segment_fingerprint,
)
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext


@dataclass(frozen=True)
class LedgerTailBoundary:
    target: UsageLedgerHead
    previous: LedgerSegmentReference | None


class LedgerSegmentService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        configuration: LedgerSegmentConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 30000:
            raise ValueError("ledger segments require a bounded timeout")
        self.sessions, self.configuration = sessions, configuration
        self.clock, self.timeout_ms = clock or (lambda: datetime.now(UTC)), timeout_ms

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger segment clock requires timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = (
            "RETRYABLE_DATABASE_ERROR"
            if getattr(error.orig, "sqlstate", None) == "23505"
            else database_failure(error)
        )
        return DomainError(code, "Ledger segment database is unavailable", 503)

    def _begin(self, db: Session, *, readonly: bool = False) -> None:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            if readonly:
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        elif dialect == "sqlite":
            db.execute(text("BEGIN" if readonly else "BEGIN IMMEDIATE"))
        else:
            raise RuntimeError("unsupported ledger segment database")
        transaction_budget(db, 100, 1000, self.timeout_ms)

    @staticmethod
    def _within(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Ledger segment budget exceeded", 503)

    def exact(self, id: UUID) -> StoredLedgerSegment:
        try:
            with self.sessions() as db:
                self._begin(db, readonly=True)
                value = LedgerSegmentRepository(db).get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Ledger segment not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _previous(
        repo: LedgerSegmentRepository,
        request: LedgerRangeBoundary,
        config: LedgerSegmentConfiguration,
    ) -> StoredLedgerSegment | None:
        ref = request.previous
        if ref is None:
            return None
        value = repo.get(ref.id)
        if value is None or value.reference() != ref or value.configuration != config:
            raise DomainError("CONFLICT", "Previous ledger segment differs", 409)
        return value

    def _source(
        self,
        db: Session,
        request: LedgerRangeBoundary,
        config: LedgerSegmentConfiguration,
        *,
        at: datetime,
        deadline: float,
    ) -> tuple[UsageLedgerEntry, ...]:
        previous = request.previous
        first = (previous.head.revision if previous else 0) + 1
        if (
            request.target.node_id != config.node_id
            or not 0 < request.target.revision - first + 1 <= config.max_entries
        ):
            raise DomainError(
                "VALIDATION_ERROR", "Ledger segment has a different bounded range", 422
            )
        repo = LedgerSegmentRepository(db)
        entries, size = repo.entries(config.node_id, first, request.target.revision)
        originals = {value.id: value for value in entries}
        missing = tuple(
            sorted(
                {
                    value.details.target_id
                    for value in entries
                    if isinstance(value.details, ReversedMeasurement)
                    and value.details.target_id not in originals
                },
                key=str,
            )
        )
        targets = repo.targets(missing, MAX_LEDGER_SEGMENT_SOURCE_BYTES - size)
        originals.update((value.id, value) for value in targets)
        fingerprint = previous.head.fingerprint if previous else None
        last_at = previous.last_recorded_at if previous else None
        for revision, entry in enumerate(entries, start=first):
            if (
                entry.node_id != config.node_id
                or entry.revision != revision
                or entry.previous_fingerprint != fingerprint
                or entry.recorded_at > at
                or (last_at is not None and entry.recorded_at < last_at)
            ):
                raise segment_inconsistent()
            if isinstance(entry.details, ReversedMeasurement):
                original = originals.get(entry.details.target_id)
                if (
                    original is None
                    or not isinstance(original.details, PostedMeasurement)
                    or original.node_id != config.node_id
                    or original.revision >= entry.revision
                    or original.fingerprint != entry.details.target_fingerprint
                    or MeasuredWindow.model_validate(
                        original.details.measurement.model_dump(
                            include=set(MeasuredWindow.model_fields)
                        )
                    )
                    != entry.details.measurement
                ):
                    raise segment_inconsistent()
            fingerprint, last_at = entry.fingerprint, entry.recorded_at
            self._within(deadline)
        if fingerprint != request.target.fingerprint or repo.has_historical_overlap(
            config.node_id, first, request.target.revision
        ):
            raise segment_inconsistent()
        self._within(deadline)
        return entries

    def verify_tail_in_transaction(
        self,
        db: Session,
        target: UsageLedgerHead,
        previous: LedgerSegmentReference | None,
        configuration: LedgerSegmentConfiguration,
        *,
        at: datetime,
        deadline: float,
    ) -> tuple[UsageLedgerEntry, ...]:
        """Verify original bounded data in the caller's read transaction; no capture or event."""
        config = LedgerSegmentConfiguration.model_validate(configuration.model_dump(mode="json"))
        target = UsageLedgerHead.model_validate(target.model_dump(mode="json"))
        boundary = LedgerTailBoundary(target, previous)
        self._previous(LedgerSegmentRepository(db), boundary, config)
        before = previous.head if previous else UsageLedgerHead(node_id=config.node_id, revision=0)
        if target == before:
            return ()
        return self._source(db, boundary, config, at=at, deadline=deadline)

    def capture(
        self, context: UsageTraceConsumeContext, request: CaptureLedgerSegment
    ) -> StoredLedgerSegment:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Ledger segment capture is paused", 503)
        config = LedgerSegmentConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        request = CaptureLedgerSegment.model_validate(request.model_dump(mode="json"))
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        if (
            request.expected_configuration_fingerprint != config.fingerprint
            or request.target.node_id != config.node_id
        ):
            raise DomainError("CONFLICT", "Ledger segment configuration differs", 409)
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                self._begin(db)
                ledger = UsageLedgerRepository(db)
                ledger.lock("node", config.node_id)
                repo = LedgerSegmentRepository(db)
                existing = repo.get(request.id)
                if existing is not None:
                    if existing.request != request or existing.configuration != config:
                        raise DomainError(
                            "CONFLICT", "Ledger segment operation has different input", 409
                        )
                    return existing
                previous = self._previous(repo, request, config)
                latest = repo.latest(config.node_id)
                if latest != previous:
                    raise DomainError("CONFLICT", "Ledger segment frontier has changed", 409)
                head = ledger.head(config.node_id)
                if request.target.revision > head.revision:
                    raise DomainError("CONFLICT", "Ledger segment exceeds the committed head", 409)
                created_at = self._now()
                entries = self._source(db, request, config, at=created_at, deadline=deadline)
                draft = StoredLedgerSegment.model_construct(
                    request=request,
                    configuration=config,
                    entries=tuple(ledger_entry_index(value) for value in entries),
                    event_id=uuid4(),
                    created_at=created_at,
                    fingerprint="0" * 64,
                )
                if len(canonical_ledger(draft).encode()) > MAX_LEDGER_SEGMENT_BYTES:
                    raise DomainError(
                        "LEDGER_SEGMENT_TOO_LARGE", "Split the verified ledger index batch", 503
                    )
                value = StoredLedgerSegment.model_validate(
                    {**draft.model_dump(), "fingerprint": ledger_segment_fingerprint(draft)}
                )
                action = "usage.ledger_segment.verified"
                payload = {
                    "id": str(request.id),
                    "node_id": config.node_id,
                    "first_revision": entries[0].revision,
                    "last_revision": entries[-1].revision,
                    "fingerprint": value.fingerprint,
                    "verification_scope": value.verification_scope,
                    "quota_usage_available": False,
                }
                db.add(
                    DomainEvent(
                        event_id=str(value.event_id),
                        event_type=action,
                        event_version=1,
                        aggregate_type="usage_ledger_segment",
                        aggregate_id=str(request.id),
                        aggregate_version=1,
                        occurred_at=created_at,
                        recorded_at=created_at,
                        actor=context.actor,
                        correlation_id=context.request_id,
                        causation_id=str(request.id),
                        payload=payload,
                    )
                )
                db.flush()
                repo.append(value)
                db.add(OutboxEntry(event_id=str(value.event_id), created_at=created_at))
                db.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=context.actor,
                        action=action,
                        target=str(request.id),
                        request_id=context.request_id,
                        recorded_at=created_at,
                        changes=payload,
                    )
                )
                db.flush()
                self._within(deadline)
                if self.configuration != config:
                    raise DomainError(
                        "CONFLICT", "Ledger segment capture configuration changed", 409
                    )
                commit_owned_transaction(db)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify(self, id: UUID) -> StoredLedgerSegment:
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                self._begin(db, readonly=True)
                repo = LedgerSegmentRepository(db)
                value = repo.get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Ledger segment not found", 404)
                self._previous(repo, value.request, value.configuration)
                entries = self._source(
                    db, value.request, value.configuration, at=value.created_at, deadline=deadline
                )
                if tuple(ledger_entry_index(entry) for entry in entries) != value.entries:
                    raise segment_inconsistent()
                return value
        except DBAPIError as error:
            raise self._failure(error) from None
