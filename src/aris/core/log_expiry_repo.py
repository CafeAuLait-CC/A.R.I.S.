from collections.abc import Iterator
from typing import cast as type_cast

from sqlalchemy import String, Table, and_, cast, delete, func, select
from sqlalchemy.orm import Session

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.log_expiry_schemas import AuditSnapshot, LogPair, LogScope
from aris.core.relay.models import EventPublication
from aris.core.relay.schemas import EventEnvelope
from aris.core.time import utc


def audit_snapshot(row: AuditEntry) -> AuditSnapshot:
    return AuditSnapshot(
        id=row.id,
        actor=row.actor,
        action=row.action,
        target=row.target,
        request_id=row.request_id,
        recorded_at=utc(row.recorded_at),
        changes=row.changes,
    )


def event_snapshot(row: DomainEvent) -> EventEnvelope:
    values = {name: getattr(row, name) for name in EventEnvelope.model_fields}
    values.update(occurred_at=utc(row.occurred_at), recorded_at=utc(row.recorded_at))
    return EventEnvelope.model_validate(values)


class LogExpiryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def pairs(self, scope: LogScope, *, audit_sequence_key: str, maximum: int) -> Iterator[LogPair]:
        event, audit = DomainEvent, AuditEntry
        statement = (
            select(event, audit)
            .join(
                audit,
                and_(
                    audit.actor == event.actor,
                    audit.action == event.event_type,
                    audit.target == event.aggregate_id,
                    audit.request_id == event.correlation_id,
                    cast(audit.changes[audit_sequence_key].as_string(), String)
                    == cast(event.aggregate_version, String),
                ),
            )
            .where(
                event.event_type.in_(scope.event_types),
                event.event_version == 1,
                event.aggregate_type == scope.aggregate_type,
                event.aggregate_id == scope.aggregate_id,
                event.actor == scope.actor,
                event.aggregate_version.between(1, scope.through_seq),
                event.occurred_at < scope.before,
                event.recorded_at < scope.before,
                audit.recorded_at < scope.before,
            )
            .order_by(event.aggregate_version, event.event_id, audit.id)
            .limit(maximum + 1)
            .execution_options(yield_per=1024, stream_results=True)
        )
        for index, (source, history) in enumerate(self.session.execute(statement)):
            if index == maximum:
                raise ValueError("Log candidate scope exceeds its explicit maximum")
            yield LogPair(event=event_snapshot(source), audit=audit_snapshot(history))

    def locked(
        self, ids: tuple[str, ...], audit_ids: tuple[str, ...]
    ) -> tuple[list[OutboxEntry], list[DomainEvent], list[AuditEntry]]:
        # The publisher locks Outbox before adding an FK to its source event.
        # Use that order so publication cannot deadlock behind our source lock.
        outbox = list(
            self.session.scalars(
                select(OutboxEntry)
                .where(OutboxEntry.event_id.in_(ids))
                .order_by(OutboxEntry.event_id)
                .with_for_update()
            )
        )
        events = list(
            self.session.scalars(
                select(DomainEvent)
                .where(DomainEvent.event_id.in_(ids))
                .order_by(DomainEvent.event_id)
                .with_for_update()
            )
        )
        audits = list(
            self.session.scalars(
                select(AuditEntry)
                .where(AuditEntry.id.in_(audit_ids))
                .order_by(AuditEntry.id)
                .with_for_update()
            )
        )
        return outbox, events, audits

    def referenced_publication(self, ids: tuple[str, ...]) -> bool:
        return (
            self.session.scalar(
                select(EventPublication.event_id).where(EventPublication.event_id.in_(ids)).limit(1)
            )
            is not None
        )

    def remaining(self, ids: tuple[str, ...], audit_ids: tuple[str, ...]) -> bool:
        return bool(
            self.session.scalar(
                select(func.count()).select_from(DomainEvent).where(DomainEvent.event_id.in_(ids))
            )
            or self.session.scalar(
                select(func.count()).select_from(AuditEntry).where(AuditEntry.id.in_(audit_ids))
            )
        )

    def delete(self, ids: tuple[str, ...], audit_ids: tuple[str, ...]) -> None:
        for model, column, expected in (
            (OutboxEntry, "event_id", ids),
            (DomainEvent, "event_id", ids),
            (AuditEntry, "id", audit_ids),
        ):
            table = type_cast(Table, model.__table__)
            returned = self.session.scalars(
                delete(table).where(table.c[column].in_(expected)).returning(table.c[column])
            ).all()
            if set(returned) != set(expected):
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Log expiry records changed during removal", 503
                )
