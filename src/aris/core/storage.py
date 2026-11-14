"""Read-only PostgreSQL storage metadata, independent of concrete domains."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime
from sqlalchemy import Engine, func, select, text

from aris.core.contracts import Contract
from aris.core.events import OutboxEntry
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
