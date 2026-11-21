"""One bounded index batch after recording, without creating another ledger or worker queue."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.ledger_schemas import UsageLedgerHead, UsagePostingContext
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository
from aris.modules.lyra.ledger_segment_schemas import (
    CaptureLedgerSegment,
    LedgerSegmentReference,
    ledger_read_configuration,
)
from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from aris.modules.lyra.usage_intervals import IntervalContract

_NAMESPACE = UUID("92f6275d-14c8-4b43-bdee-e04c42e69ce5")


class LedgerIndexStep(IntervalContract):
    state: Literal["within_tail", "advanced"]
    node_id: str
    observed_head: UsageLedgerHead
    before_revision: int = Field(ge=0, strict=True)
    indexed_revision: int = Field(ge=0, strict=True)
    remaining_entries: int = Field(ge=0, strict=True)
    batch_entries: int = Field(ge=1, le=128, strict=True)
    segment: LedgerSegmentReference | None = None
    generated_at: AwareDatetime
    quota_usage_available: Literal[False] = False

    @model_validator(mode="after")
    def observed_progress(self) -> "LedgerIndexStep":
        if (
            self.observed_head.node_id != self.node_id
            or not self.before_revision <= self.indexed_revision <= self.observed_head.revision
            or self.remaining_entries != self.observed_head.revision - self.indexed_revision
            or (self.state == "advanced") != (self.segment is not None)
            or (
                self.state == "within_tail"
                and (
                    self.indexed_revision != self.before_revision
                    or self.remaining_entries >= self.batch_entries
                )
            )
            or (
                self.segment is not None
                and (
                    self.segment.head.node_id != self.node_id
                    or self.segment.head.revision != self.indexed_revision
                    or self.indexed_revision - self.before_revision != self.batch_entries
                )
            )
        ):
            raise ValueError("ledger index progress differs from its observed original range")
        return self


class LedgerIndexMaintenance:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        node_ids: tuple[str, ...],
        *,
        clock: Callable[[], datetime] | None = None,
        batch_entries: int = 128,
        idle_seconds: int = 30,
    ) -> None:
        if not 1 <= len(node_ids) <= 16 or len(set(node_ids)) != len(node_ids):
            raise ValueError("ledger index maintenance requires unique configured nodes")
        if type(batch_entries) is not int or not 1 <= batch_entries <= 128:
            raise ValueError("ledger index maintenance requires a bounded full batch")
        if type(idle_seconds) is not int or not 1 <= idle_seconds <= 60:
            raise ValueError("ledger index maintenance requires a bounded idle interval")
        self.sessions, self.node_ids = sessions, tuple(node_ids)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.batch_entries, self.idle_seconds = batch_entries, idle_seconds
        self._sizes: dict[str, int] = {}
        self._not_before: dict[str, datetime] = {}
        self._next = 0
        self.services = {
            node: LedgerSegmentService(
                sessions,
                configuration=ledger_read_configuration(node),
                clock=self.clock,
                timeout_ms=10000,
            )
            for node in node_ids
        }

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger index clock requires timezone")
        return value.astimezone(UTC)

    def step(
        self, context: UsagePostingContext, *, activity: bool = False
    ) -> LedgerIndexStep | None:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        now = self._now()
        node = None
        for _ in self.node_ids:
            candidate = self.node_ids[self._next % len(self.node_ids)]
            self._next += 1
            due = self._not_before.get(candidate)
            if activity or due is None or due <= now:
                node = candidate
                break
        if node is None:
            return None
        self._not_before[node] = now + timedelta(seconds=self.idle_seconds)
        service = self.services[node]
        config = ledger_read_configuration(node)
        batch = self._sizes.get(node, self.batch_entries)
        try:
            with self.sessions() as db:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                elif db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN"))
                transaction_budget(db, 100, 1000, 10000)
                ledger, segments = UsageLedgerRepository(db), LedgerSegmentRepository(db)
                head = ledger.head(node)
                position = segments.latest_position(node)
                before = position[1] if position else 0
                if before > head.revision or (position and position[2] != config.fingerprint):
                    raise inconsistent()
                if head.revision - before < batch:
                    return LedgerIndexStep(
                        state="within_tail",
                        node_id=node,
                        observed_head=head,
                        before_revision=before,
                        indexed_revision=before,
                        remaining_entries=head.revision - before,
                        batch_entries=batch,
                        generated_at=self._now(),
                    )
                previous = segments.get(position[0]) if position else None
                if position and (previous is None or previous.configuration != config):
                    raise inconsistent()
                point = ledger.revision(node, before + batch)
                if point is None:
                    raise inconsistent()
                target = UsageLedgerHead(
                    node_id=node, revision=point.revision, fingerprint=point.fingerprint
                )
            value = service.capture(
                UsageTraceConsumeContext(**context.model_dump()),
                CaptureLedgerSegment(
                    id=uuid5(_NAMESPACE, config.fingerprint + ":" + point.fingerprint),
                    target=target,
                    previous=previous.reference() if previous else None,
                    expected_configuration_fingerprint=config.fingerprint,
                ),
            )
            remaining = head.revision - value.request.target.revision
            if remaining >= batch:
                self._not_before[node] = now
            return LedgerIndexStep(
                state="advanced",
                node_id=node,
                observed_head=head,
                before_revision=before,
                indexed_revision=value.request.target.revision,
                remaining_entries=remaining,
                batch_entries=batch,
                segment=value.reference(),
                generated_at=self._now(),
            )
        except DomainError as error:
            if error.code == "LEDGER_SEGMENT_TOO_LARGE" and batch > 1:
                self._sizes[node] = max(1, batch // 2)
                self._not_before[node] = now
            raise
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Ledger index maintenance database is unavailable", 503
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None
