"""Original-source capture and payload retirement share a durable row boundary."""

from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


def protect_archive_sources_in_transaction(
    session: Session, source: UsageArchiveHeadQuery, *, after_seq: int | None = None
) -> int:
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    retired = VegaRepository(session).protect_archive_source(source.agent_id, source.node_id)
    if after_seq is not None and after_seq < retired:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Requested archive sources have retired payloads", 503
        )
    return retired


def lock_archive_retirement_in_transaction(session: Session, source: UsageArchiveHeadQuery) -> int:
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    return VegaRepository(session).lock_archive_retirement(source.agent_id, source.node_id)
