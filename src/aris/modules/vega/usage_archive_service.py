"""Read-only source ceilings and original pages; never skip a sequence gap."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.repo import StoredUsageMessage, VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.storage_schemas import (
    ObservedWindowStorage,
    RetentionPrefixPreview,
    RetentionPrefixStop,
)
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_MESSAGES,
    MAX_ARCHIVE_SEQ,
    MAX_ARCHIVE_SOURCE_CHARACTERS,
    ArchivedReportingPage,
    ArchivedUsageItem,
    ArchivedUsageReference,
    UsageArchiveFreeze,
    UsageArchiveHead,
    UsageArchiveHeadQuery,
    UsageArchivePage,
    UsageArchiveQuery,
    archive_head_fingerprint,
    archive_page_fingerprint,
)
from aris.modules.vega.usage_service import _original_report, _usage_fact


def _archived_item(row: StoredUsageMessage, query: UsageArchiveQuery) -> ArchivedUsageItem:
    batch = _original_report(row, query.agent_id, query.node_id)
    fingerprint = row.fingerprint
    return ArchivedUsageItem(
        reference=ArchivedUsageReference(
            agent_id=row.agent_id,
            node_id=row.node_id,
            message_id=row.message_id,
            seq=row.seq,
            kind=row.message_kind,
            fingerprint=fingerprint,
            observed_at=utc(row.observed_at),
            received_at=utc(row.received_at),
        ),
        observation=(
            _usage_fact(batch, fingerprint=fingerprint, received_at=utc(row.received_at))
            if isinstance(batch, ResourceTelemetryBatch)
            else None
        ),
    )


def _archived_reporting_reference(
    row: StoredUsageMessage, query: UsageArchiveQuery
) -> ArchivedUsageReference:
    payload = row.payload
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    kind = payload.get("type")
    if kind is None and row.message_kind == "telemetry" and isinstance(payload.get("gpus"), list):
        kind = "telemetry"
    reference = ArchivedUsageReference(
        agent_id=payload["agent_id"],
        node_id=payload["node_id"],
        message_id=payload["message_id"],
        seq=payload["seq"],
        kind=kind,
        fingerprint=fingerprint,
        observed_at=payload["observed_at"],
        received_at=utc(row.received_at),
    )
    if (row.agent_id, row.node_id) != (query.agent_id, query.node_id) or (
        reference.agent_id,
        reference.node_id,
        str(reference.message_id),
        reference.seq,
        reference.kind,
        reference.observed_at,
        reference.fingerprint,
    ) != (
        row.agent_id,
        row.node_id,
        row.message_id,
        row.seq,
        row.message_kind,
        utc(row.observed_at),
        row.fingerprint,
    ):
        raise ValueError("Reporting reference differs from the verified archived envelope")
    return reference


class VegaUsageArchive:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 3000,
    ) -> None:
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000:
            raise ValueError("usage archive requires a bounded read timeout")
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def observed_window_storage_in_transaction(
        self, session: Session, query: UsageArchiveHeadQuery, start: datetime, end: datetime
    ) -> ObservedWindowStorage:
        """Count packet metadata by reported observation time, without reading payloads."""
        query = UsageArchiveHeadQuery.model_validate(query.model_dump(mode="json"))
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError("Storage preview requires a positive aware interval")
        repo = VegaRepository(session)
        if repo.usage_source_node(query.agent_id) != query.node_id:
            raise DomainError("CONFLICT", "Archived usage source binding differs", 409)
        return repo.observed_window_storage(query.agent_id, query.node_id, start, end)

    def arrivals_outside_freezes_in_transaction(
        self, session: Session, freezes: tuple[UsageArchiveFreeze, ...]
    ) -> tuple[int, ...]:
        if len(freezes) > 7 or any(value.source != freezes[0].source for value in freezes[1:]):
            raise ValueError("Archive arrivals require up to seven days of one source")
        return VegaRepository(session).arrivals_outside_freezes(
            tuple(
                UsageArchiveFreeze.model_validate(value.model_dump(mode="json"))
                for value in freezes
            )
        )

    def retention_prefix_in_transaction(
        self,
        session: Session,
        query: UsageArchiveHeadQuery,
        *,
        start: datetime,
        end: datetime,
        through_seq: int,
        limit: int = MAX_ARCHIVE_MESSAGES,
    ) -> RetentionPrefixPreview:
        """Describe the oldest contiguous, unprotected payloads using metadata alone."""
        query = UsageArchiveHeadQuery.model_validate(query.model_dump(mode="json"))
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise ValueError("Retention preview requires a positive aware interval")
        if (
            type(through_seq) is not int
            or not 0 <= through_seq <= MAX_ARCHIVE_SEQ
            or type(limit) is not int
            or not 1 <= limit <= MAX_ARCHIVE_MESSAGES
        ):
            raise ValueError("Retention preview requires a bounded sequence range")
        with session.no_autoflush:
            repo = VegaRepository(session)
            if repo.usage_source_node(query.agent_id) != query.node_id:
                raise DomainError("CONFLICT", "Archived usage source binding differs", 409)
            rows = repo.oldest_payload_metadata(query.agent_id, limit + 1)
            if not rows:
                return RetentionPrefixPreview(stop_reason="payload_end")
            first = rows[0][0]
            if first < 1 or (
                first > 1 and repo.retired_sequence_count_before(query.agent_id, first) != first - 1
            ):
                return RetentionPrefixPreview(
                    stop_reason="unverified_prior_sequences", stopped_before_seq=first
                )
        reason: RetentionPrefixStop = "payload_end"
        expected = first
        for seq, node, observed, received in rows:
            if seq != expected:
                reason = "sequence_gap"
            elif node != query.node_id:
                reason = "source_binding_differs"
            elif not start <= observed < end:
                reason = "outside_observation_window"
            elif received >= end:
                reason = "received_after_cutoff"
            elif seq > through_seq:
                reason = "protected_sequence"
            elif expected - first == limit:
                reason = "batch_limit"
            else:
                expected += 1
                continue
            break
        return RetentionPrefixPreview(
            first_seq=first if expected > first else None,
            through_seq=expected - 1 if expected > first else None,
            stop_reason=reason,
            stopped_before_seq=expected if reason != "payload_end" else None,
        )

    def head(self, query: UsageArchiveHeadQuery) -> UsageArchiveHead:
        try:
            with self.sessions() as session:
                transaction_budget(
                    session, min(100, self.timeout_ms), min(1000, self.timeout_ms), self.timeout_ms
                )
                return self.head_in_transaction(session, query)
        except DBAPIError as error:
            raise self._database_failure(error) from None

    def head_in_transaction(
        self, session: Session, query: UsageArchiveHeadQuery
    ) -> UsageArchiveHead:
        query = UsageArchiveHeadQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                row = VegaRepository(session).usage_source_ceiling(query.agent_id)
                if row is None:
                    raise DomainError("NOT_FOUND", "Archived usage source is unknown", 404)
                node_id, projected, archived = row
                if node_id != query.node_id:
                    raise DomainError("CONFLICT", "Archived usage source binding differs", 409)
                if (archived is not None and archived < 1) or projected != (
                    archived if archived is not None else 0
                ):
                    raise ValueError("archive source ceiling differs from its node projection")
                now = self.clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("usage archive clock requires a timezone")
                unchecked = UsageArchiveHead.model_construct(
                    query=query,
                    highest_committed_seq=projected,
                    read_at=utc(now),
                    fingerprint="0" * 64,
                )
                result = UsageArchiveHead.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": archive_head_fingerprint(unchecked),
                    }
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Archive head read budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            raise self._database_failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Original archive source ceiling is inconsistent", 503
            ) from None

    def page(self, query: UsageArchiveQuery) -> UsageArchivePage:
        try:
            with self.sessions() as session:
                transaction_budget(
                    session, min(100, self.timeout_ms), min(1000, self.timeout_ms), self.timeout_ms
                )
                return self.page_in_transaction(session, query)
        except DBAPIError as error:
            raise self._database_failure(error) from None

    @staticmethod
    def _database_failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Usage archive database is unavailable",
            503,
        )

    @staticmethod
    def _source_rows(
        session: Session, query: UsageArchiveQuery
    ) -> tuple[tuple[StoredUsageMessage, ...], int]:
        repo = VegaRepository(session)
        binding = repo.usage_source_node(query.agent_id)
        if binding is None:
            raise DomainError("NOT_FOUND", "Archived usage source is unknown", 404)
        if binding != query.node_id:
            raise DomainError("CONFLICT", "Archived usage source binding differs", 409)
        metadata = repo.usage_range_metadata(
            query.agent_id, query.after_seq, query.page_through_seq
        )
        selected = []
        characters = 0
        for index, (message_id, seq, size) in enumerate(metadata, start=1):
            if seq != query.after_seq + index:
                break
            characters += size
            if characters > min(MAX_ARCHIVE_SOURCE_CHARACTERS, query.source_character_budget):
                raise DomainError(
                    "USAGE_ARCHIVE_TOO_LARGE", "Archive page exceeds its source budget", 503
                )
            selected.append(message_id)
        rows = repo.usage_messages(query.agent_id, tuple(selected)) if selected else ()
        if len(rows) != len(selected) or tuple(row.message_id for row in rows) != tuple(selected):
            raise ValueError("archived source prefix changed while loading")
        return rows, characters

    def reporting_page_in_transaction(
        self, session: Session, query: UsageArchiveQuery
    ) -> ArchivedReportingPage:
        """Verify original envelope timing for health history without deriving usage."""
        query = UsageArchiveQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                rows, _ = self._source_rows(session, query)
                page = ArchivedReportingPage(
                    query=query,
                    reports=tuple(_archived_reporting_reference(row, query) for row in rows),
                    read_at=self.clock(),
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR",
                        "Archive report timing read budget exceeded",
                        503,
                    )
                return page
        except DBAPIError as error:
            raise self._database_failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Original reporting envelope is inconsistent", 503
            ) from None

    def page_in_transaction(self, session: Session, query: UsageArchiveQuery) -> UsageArchivePage:
        query = UsageArchiveQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                rows, characters = self._source_rows(session, query)
                items = tuple(_archived_item(row, query) for row in rows)
                end = query.after_seq + len(items)
                state: Literal["range_end", "page_end", "gap"] = (
                    "range_end"
                    if end == query.through_seq
                    else ("page_end" if end == query.page_through_seq else "gap")
                )
                now = self.clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("usage archive clock requires a timezone")
                unchecked = UsageArchivePage.model_construct(
                    query=query,
                    items=items,
                    source_characters=characters,
                    next_after_seq=end,
                    state=state,
                    missing_seq=end + 1 if state == "gap" else None,
                    read_at=utc(now),
                    fingerprint="0" * 64,
                )
                result = UsageArchivePage.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": archive_page_fingerprint(unchecked),
                    }
                )
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Archive page read budget exceeded", 503
                    )
                return result
        except DBAPIError as error:
            raise self._database_failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Original usage archive source is inconsistent", 503
            ) from None
