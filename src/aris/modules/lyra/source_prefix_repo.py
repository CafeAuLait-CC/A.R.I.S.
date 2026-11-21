"""Bounded immutable source checkpoints; no mutable cursor and no source deletion."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import SourcePrefixCheckpointRow
from aris.modules.lyra.source_prefix_schemas import (
    MAX_SOURCE_PREFIX_BYTES,
    SourcePrefixReference,
    StoredSourcePrefix,
)
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConfiguration, canonical_trace


class SourcePrefixRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id: UUID) -> StoredSourcePrefix | None:
        row = (
            self.session.execute(
                select(*SourcePrefixCheckpointRow.__table__.c).where(
                    SourcePrefixCheckpointRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_checkpoint"]
            if len(raw.encode()) > MAX_SOURCE_PREFIX_BYTES:
                raise ValueError("oversized prefix checkpoint")
            value = StoredSourcePrefix.model_validate_json(raw)
            if canonical_trace(value) != raw or (
                str(value.request.id),
                str(value.event_id),
                str(value.request.previous.id) if value.request.previous else None,
                value.configuration.consumer_id,
                value.configuration.fingerprint,
                value.proofs[-1].revision,
                value.max_observed_at,
                value.fingerprint,
                value.created_at,
            ) != (
                row["id"],
                row["event_id"],
                row["previous_id"],
                row["consumer_id"],
                row["configuration_fingerprint"],
                row["through_revision"],
                utc(row["max_observed_at"]) if row["max_observed_at"] is not None else None,
                row["fingerprint"],
                utc(row["created_at"]),
            ):
                raise ValueError("prefix checkpoint metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Source prefix checkpoint is inconsistent", 503
            ) from None

    def checked(
        self,
        reference: SourcePrefixReference,
        config: UsageTraceConfiguration,
        start_at: datetime,
    ) -> StoredSourcePrefix:
        value = self.get(reference.id)
        if value is None or value.reference() != reference or value.configuration != config:
            raise DomainError("CONFLICT", "Source prefix checkpoint binding differs", 409)
        if value.max_observed_at is not None and value.max_observed_at >= start_at:
            raise DomainError("CONFLICT", "Source prefix may affect the selected window", 409)
        return value

    def latest(
        self, config: UsageTraceConfiguration, through_revision: int
    ) -> StoredSourcePrefix | None:
        id = self.session.scalar(
            select(SourcePrefixCheckpointRow.id)
            .where(
                SourcePrefixCheckpointRow.consumer_id == config.consumer_id,
                SourcePrefixCheckpointRow.configuration_fingerprint == config.fingerprint,
                SourcePrefixCheckpointRow.through_revision <= through_revision,
            )
            .order_by(
                SourcePrefixCheckpointRow.through_revision.desc(), SourcePrefixCheckpointRow.id
            )
            .limit(1)
        )
        return self.get(UUID(id)) if id is not None else None

    def eligible(
        self, config: UsageTraceConfiguration, start_at: datetime, through_revision: int
    ) -> StoredSourcePrefix | None:
        id = self.session.scalar(
            select(SourcePrefixCheckpointRow.id)
            .where(
                SourcePrefixCheckpointRow.consumer_id == config.consumer_id,
                SourcePrefixCheckpointRow.configuration_fingerprint == config.fingerprint,
                SourcePrefixCheckpointRow.through_revision <= through_revision,
                or_(
                    SourcePrefixCheckpointRow.max_observed_at.is_(None),
                    SourcePrefixCheckpointRow.max_observed_at < start_at,
                ),
            )
            .order_by(
                SourcePrefixCheckpointRow.through_revision.desc(), SourcePrefixCheckpointRow.id
            )
            .limit(1)
        )
        if id is None:
            return None
        value = self.get(UUID(id))
        if value is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Source prefix disappeared", 503)
        self.checked(value.reference(), config, start_at)
        return value

    def append(self, value: StoredSourcePrefix) -> None:
        self.session.add(
            SourcePrefixCheckpointRow(
                id=str(value.request.id),
                event_id=str(value.event_id),
                previous_id=str(value.request.previous.id) if value.request.previous else None,
                consumer_id=value.configuration.consumer_id,
                configuration_fingerprint=value.configuration.fingerprint,
                through_revision=value.proofs[-1].revision,
                max_observed_at=value.max_observed_at,
                fingerprint=value.fingerprint,
                canonical_checkpoint=canonical_trace(value),
                created_at=value.created_at,
            )
        )
