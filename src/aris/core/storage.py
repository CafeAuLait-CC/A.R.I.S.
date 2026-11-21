"""Read-only PostgreSQL storage metadata, independent of concrete domains."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import Engine, func, select, text

from aris.core.contracts import Contract
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc


class RelationStorage(Contract):
    table_name: str
    total_bytes: int
    index_bytes: int
    estimated_live_rows: int
    estimated_dead_rows: int


class StorageSnapshot(Contract):
    generated_at: AwareDatetime
    status: Literal["measured", "unsupported"]
    relation_bytes: int | None = None
    database_bytes: int | None = None
    rows_are_estimates: Literal[True] = True
    pending_domain_events: int
    oldest_pending_event_at: AwareDatetime | None
    relations: list[RelationStorage]


class PendingEventType(Contract):
    event_type: str | None
    pending_count: int = Field(gt=0)
    oldest_created_at: AwareDatetime


class PendingEventInventory(Contract):
    assessment_scope: Literal["pending_domain_event_inventory"] = "pending_domain_event_inventory"
    generated_at: AwareDatetime
    read_only: Literal[True] = True
    deletion_enabled: Literal[False] = False
    total_pending: int = Field(ge=0)
    oldest_created_at: AwareDatetime | None
    event_types: tuple[PendingEventType, ...] = Field(max_length=64)
    truncated: bool


def inspect_pending_events(engine: Engine, *, type_limit: int = 64) -> PendingEventInventory:
    """One metadata-only statement keeps totals exact even when type details are bounded."""
    if type(type_limit) is not int or not 1 <= type_limit <= 64:
        raise ValueError("event type limit must be an integer from 1 to 64")
    count, oldest = func.count(), func.min(OutboxEntry.created_at)
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                DomainEvent.event_type,
                count,
                oldest,
                func.sum(count).over(),
                func.min(oldest).over(),
            )
            .select_from(OutboxEntry)
            .outerjoin(DomainEvent, DomainEvent.event_id == OutboxEntry.event_id)
            .where(OutboxEntry.published_at.is_(None))
            .group_by(DomainEvent.event_type)
            .order_by(count.desc(), DomainEvent.event_type.asc().nulls_last())
            .limit(type_limit + 1)
        ).all()
    return PendingEventInventory(
        generated_at=datetime.now(UTC),
        total_pending=int(rows[0][3]) if rows else 0,
        oldest_created_at=utc(rows[0][4]) if rows else None,
        event_types=tuple(
            PendingEventType(event_type=row[0], pending_count=row[1], oldest_created_at=utc(row[2]))
            for row in rows[:type_limit]
        ),
        truncated=len(rows) > type_limit,
    )


def inspect_storage(engine: Engine) -> StorageSnapshot:
    with engine.connect() as connection:
        pending = connection.execute(
            select(func.count(), func.min(OutboxEntry.created_at))
            .select_from(OutboxEntry)
            .where(OutboxEntry.published_at.is_(None))
        ).one()
        oldest = utc(pending[1]) if pending[1] else None
        if engine.dialect.name != "postgresql":
            return StorageSnapshot(
                generated_at=datetime.now(UTC),
                status="unsupported",
                pending_domain_events=pending[0],
                oldest_pending_event_at=oldest,
                relations=[],
            )
        rows = connection.execute(
            text(
                "SELECT relname AS table_name, pg_total_relation_size(relid) AS total_bytes, "
                "pg_indexes_size(relid) AS index_bytes, n_live_tup AS estimated_live_rows, "
                "n_dead_tup AS estimated_dead_rows FROM pg_stat_user_tables "
                "WHERE schemaname=current_schema() "
                "ORDER BY pg_total_relation_size(relid) DESC, relname"
            )
        ).mappings()
        relations = [RelationStorage.model_validate(dict(row)) for row in rows]
        database_bytes = connection.scalar(text("SELECT pg_database_size(current_database())"))
    return StorageSnapshot(
        generated_at=datetime.now(UTC),
        status="measured",
        relation_bytes=sum(relation.total_bytes for relation in relations),
        database_bytes=database_bytes,
        pending_domain_events=pending[0],
        oldest_pending_event_at=oldest,
        relations=relations,
    )
