"""Temporal identity facts; current mappings and active principals are separate contracts."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import AdministrationContext
from aris.core.access.history_repo import MappingHistoryRepository
from aris.core.access.history_schemas import (
    HistoricalMappingValue,
    HistoricalNodeUserQuery,
    HistoricalNodeUserResolution,
    MappingHistoryEntry,
    MappingHistoryReference,
    mapping_history_fingerprint,
)
from aris.core.contracts import DomainError
from aris.core.time import utc

MappingKey = tuple[str, str]


class NodeUserHistoryService:
    def __init__(
        self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mapping history clock requires a timezone")
        return utc(value)

    def _record_changes_in_transaction(
        self,
        session: Session,
        context: AdministrationContext,
        *,
        event_id: str,
        revision: int,
        effective_at: datetime,
        before: dict[MappingKey, HistoricalMappingValue | None],
    ) -> None:
        """Called only by the owning, already-authorized administration transaction."""
        repo = MappingHistoryRepository(session)
        for (node_id, username), previous in sorted(before.items()):
            current = repo.current(node_id, username)
            if current == previous:
                continue
            latest = repo.latest(node_id, username)
            if latest is not None:
                if latest.after != previous:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Mapping projection differs from its history", 503
                    )
                if effective_at < latest.effective_at or revision <= latest.revision:
                    raise DomainError(
                        "CONFLICT", "Mapping history clock or revision moved backwards", 409
                    )
            entry = MappingHistoryEntry(
                id=uuid4(),
                node_id=node_id,
                os_username=username,
                revision=revision,
                effective_at=effective_at,
                event_id=event_id,
                actor_id=context.actor_id,
                operation_id=context.operation_id,
                request_id=context.request_id,
                reason=context.reason,
                before=previous,
                after=current,
                fingerprint="0" * 64,
            )
            entry = entry.model_copy(update={"fingerprint": mapping_history_fingerprint(entry)})
            repo.append(entry)

    def resolve(self, query: HistoricalNodeUserQuery) -> HistoricalNodeUserResolution:
        with self.sessions() as session:
            return self.resolve_in_transaction(session, query)

    def resolve_in_transaction(
        self, session: Session, query: HistoricalNodeUserQuery
    ) -> HistoricalNodeUserResolution:
        query = HistoricalNodeUserQuery.model_validate(query.model_dump(mode="json"))
        if query.as_of > self._now():
            return HistoricalNodeUserResolution(
                query=query, status="unknown", reason="AS_OF_NOT_REACHED"
            )
        with session.no_autoflush:
            repo = MappingHistoryRepository(session)
            revision = repo.revision()
            if revision is None:
                return HistoricalNodeUserResolution(
                    query=query, status="unknown", reason="DIRECTORY_UNINITIALIZED"
                )
            bound = query.through_revision or revision
            if bound > revision:
                raise DomainError("CONFLICT", "Requested identity revision is not available", 409)
            latest = repo.latest(query.node_id, query.os_username)
            current = repo.current(query.node_id, query.os_username)
            if latest is not None and (latest.after != current or latest.revision > revision):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Mapping projection differs from its history", 503
                )
            head = repo.covering(query, bound)
            entries = (head, *repo.changes(query, bound)) if head is not None else ()
            for previous, next_entry in zip(entries, entries[1:], strict=False):
                if next_entry.revision <= previous.revision or previous.after != next_entry.before:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Mapping history chain is inconsistent", 503
                    )
            if repo.revision() != revision:
                raise DomainError(
                    "CONFLICT", "Identity directory changed during historical lookup", 409
                )
        if not entries:
            return HistoricalNodeUserResolution(
                query=query,
                directory_revision=bound,
                status="unknown",
                reason="HISTORY_DOES_NOT_COVER_INTERVAL",
            )
        evidence = tuple(
            MappingHistoryReference.model_validate(
                entry.model_dump(include={"id", "revision", "effective_at", "fingerprint"})
            )
            for entry in entries
        )
        values = []
        for index, entry in enumerate(entries):
            until = entries[index + 1].effective_at if index + 1 < len(entries) else query.end_at
            if max(query.start_at, entry.effective_at) < until:
                values.append(entry.after)
        if any(value is None for value in values):
            return HistoricalNodeUserResolution(
                query=query,
                directory_revision=bound,
                status="unknown" if all(value is None for value in values) else "needs_review",
                reason="EXPLICITLY_UNMAPPED",
                evidence=evidence,
            )
        if any(value is not None and value.confidence != "verified" for value in values):
            return HistoricalNodeUserResolution(
                query=query,
                directory_revision=bound,
                status="needs_review",
                reason="UNVERIFIED_MAPPING",
                evidence=evidence,
            )
        users = {value.user_id for value in values if value is not None}
        if len(users) != 1:
            return HistoricalNodeUserResolution(
                query=query,
                directory_revision=bound,
                status="needs_review",
                reason="MAPPING_CHANGED_DURING_INTERVAL",
                evidence=evidence,
            )
        user_id: UUID = next(iter(users))
        return HistoricalNodeUserResolution(
            query=query,
            directory_revision=bound,
            status="resolved",
            reason="VERIFIED_USER_COVERS_INTERVAL",
            user_id=user_id,
            evidence=evidence,
        )
