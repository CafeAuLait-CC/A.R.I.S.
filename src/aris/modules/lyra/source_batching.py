"""Optional source coalescing uses persisted arrival time, never an in-memory wait cursor."""

from datetime import datetime
from typing import Any, Protocol, cast

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from aris.core.contracts import DomainError
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConfiguration
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import UsageArchivePage, UsageArchiveQuery


class SourceBatchRule(IntervalContract):
    minimum_messages: int = Field(ge=1, le=128, strict=True)
    maximum_delay_seconds: int = Field(ge=1, le=3600, strict=True)


class SourceBatchSelection(IntervalContract):
    source_batching: SourceBatchRule | None = None

    @model_serializer(mode="wrap")
    def serialize_batching(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = cast(dict[str, Any], handler(self))
        if self.source_batching is None:
            result.pop("source_batching", None)
        return result


class SourceBatchPageReader(Protocol):
    def page(self, query: UsageArchiveQuery) -> UsageArchivePage: ...


def should_buffer_source(
    rule: SourceBatchRule,
    archive: SourceBatchPageReader,
    trace: UsageTraceConfiguration,
    *,
    after_seq: int,
    through_seq: int,
    now: datetime,
) -> bool:
    if through_seq - after_seq >= rule.minimum_messages:
        return False
    query = UsageArchiveQuery(
        agent_id=trace.agent_id,
        node_id=trace.node_id,
        after_seq=after_seq,
        through_seq=through_seq,
        limit=1,
    )
    try:
        page = archive.page(query)
        page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise DomainError("CONFLICT", "Batch source preview is malformed", 409) from None
    if page.query != query:
        raise DomainError("CONFLICT", "Batch source preview has different scope", 409)
    if not page.items:
        return False  # A real gap must go through the ordinary source path immediately.
    first = page.items[0].reference
    if (first.agent_id, first.node_id, first.seq) != (trace.agent_id, trace.node_id, after_seq + 1):
        raise DomainError("CONFLICT", "Batch source preview has different position", 409)
    if first.received_at > now:
        raise DomainError("CONFLICT", "Batch source arrival exceeds worker clock", 409)
    return (now - first.received_at).total_seconds() < rule.maximum_delay_seconds
