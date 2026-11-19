"""Capture consistent historical metrics and read saved results under current permissions."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_service import NodeUserHistoryService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from aris.modules.lyra.window_coverage import sampled_usage_window
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_reader_schemas import SavedWindowView
from aris.modules.lyra.window_schemas import SampledUsageWindow
from aris.modules.lyra.window_snapshot_inputs import selected_window_manifests, window_identities
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.lyra.window_snapshot_schemas import (
    CaptureUsageWindow,
    StoredUsageWindow,
    UsageWindowReceipt,
    window_snapshot_fingerprint,
)
from aris.modules.vega.usage_archive_schemas import UsageArchiveHead, UsageArchiveHeadQuery


class WindowArchiveReader(Protocol):
    def head_in_transaction(
        self, session: Session, query: UsageArchiveHeadQuery
    ) -> UsageArchiveHead: ...


class UsageWindowSnapshotService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        coverage: ProcessingCoverageService,
        archive: WindowArchiveReader,
        identities: NodeUserHistoryService,
        *,
        configuration: ProcessingCoverageConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 300000,
    ) -> None:
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 300000:
            raise ValueError("window snapshot creation requires an internal bounded timeout")
        self.sessions, self.coverage, self.archive, self.identities = (
            sessions,
            coverage,
            archive,
            identities,
        )
        self.configuration, self.timeout_ms = configuration, timeout_ms
        self.clock = clock or (lambda: datetime.now(UTC))
        self.reader = UsageWindowReader(sessions, timeout_ms=min(timeout_ms, 10000))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("window snapshot clock requires a timezone")
        return value.astimezone(UTC)

    def _begin(self, session: Session, *, read_only: bool = False) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            session.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    + (", READ ONLY" if read_only else "")
                )
            )
        elif dialect == "sqlite":
            session.execute(text("BEGIN"))
        else:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Unsupported snapshot database", 503)
        transaction_budget(session, 100, 1000, self.timeout_ms)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        # REPEATABLE READ can observe absence before an identical creator commits.
        diag = getattr(error.orig, "diag", None)
        if (
            getattr(error.orig, "sqlstate", None) == "23505"
            and getattr(diag, "constraint_name", None) == "usage_window_snapshots_pkey"
        ):
            code = "RETRYABLE_DATABASE_ERROR"
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Window snapshot database is unavailable",
            503,
        )

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Window snapshot budget exceeded", 503)

    def _build(
        self,
        session: Session,
        request: CaptureUsageWindow,
        config: ProcessingCoverageConfiguration,
        source: UsageArchiveHead,
        revision: int | None,
        read_at: datetime,
        deadline: float,
    ) -> SampledUsageWindow:
        UsageWindowRepository(session).check_manifest_bytes(request.manifests)
        manifests = selected_window_manifests(session, self.coverage, request.manifests, deadline)
        entries = UsageLedgerRepository(session).prefix(request.ledger)
        identities = window_identities(
            session, self.identities, manifests, revision, read_at, deadline
        )
        result = sampled_usage_window(
            request.query,
            config,
            source,
            request.ledger,
            entries,
            manifests,
            identities,
            identity_revision=revision,
            read_at=read_at,
        )
        self._deadline(deadline)
        return result

    def capture(
        self, context: UsageTraceConsumeContext, request: CaptureUsageWindow
    ) -> UsageWindowReceipt:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Window snapshot capture is not enabled", 503)
        config = ProcessingCoverageConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        request = CaptureUsageWindow.model_validate(request.model_dump(mode="json"))
        if request.expected_configuration_fingerprint != config.fingerprint:
            raise DomainError("CONFLICT", "Window capture configuration differs", 409)
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as session:
                self._begin(session)
                repo = UsageWindowRepository(session)
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request or prior.window.configuration != config:
                        raise DomainError("CONFLICT", "Window operation has different input", 409)
                    return UsageWindowReceipt(snapshot=prior, duplicate=True)
                source = self.archive.head_in_transaction(
                    session,
                    UsageArchiveHeadQuery(
                        agent_id=config.trace.agent_id, node_id=config.trace.node_id
                    ),
                )
                if (
                    source.fingerprint != request.expected_source_fingerprint
                    or UsageLedgerRepository(session).head(config.trace.node_id) != request.ledger
                ):
                    raise DomainError(
                        "CONFLICT", "Window capture committed boundaries changed", 409
                    )
                revision = self.identities.revision_in_transaction(session)
                read_at = self._now()
                if request.query.end_at > read_at:
                    raise DomainError("VALIDATION_ERROR", "Window capture has not ended", 422)
                result = self._build(session, request, config, source, revision, read_at, deadline)
                draft = StoredUsageWindow.model_construct(
                    request=request,
                    window=result,
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                stored = StoredUsageWindow.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": window_snapshot_fingerprint(draft),
                    }
                )
                self._append(repo, context, stored)
                session.flush()
                self._deadline(deadline)
                commit_owned_transaction(session)
                return UsageWindowReceipt(snapshot=stored, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None

    def exact(self, id: UUID) -> StoredUsageWindow:
        """Internal historical receipt. User-facing reads must call read()."""
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 10000)
                result = UsageWindowRepository(session).get(id)
                if result is None:
                    raise DomainError("NOT_FOUND", "Window snapshot not found", 404)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def read(self, actor: ActorContext, id: UUID) -> SavedWindowView:
        return self.reader.read(actor, id)

    def reverify(self, id: UUID) -> StoredUsageWindow:
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as session, session.no_autoflush:
                self._begin(session, read_only=True)
                result = UsageWindowRepository(session).get(id)
                if result is None:
                    raise DomainError("NOT_FOUND", "Window snapshot not found", 404)
                if (
                    UsageLedgerRepository(session).head(result.request.query.node_id).revision
                    < result.request.ledger.revision
                ):
                    raise DomainError(
                        "CONFLICT", "Window ledger prefix is no longer available", 409
                    )
                window = self._build(
                    session,
                    result.request,
                    result.window.configuration,
                    result.window.source,
                    result.window.identity_revision,
                    result.window.read_at,
                    deadline,
                )
                if window != result.window:
                    raise DomainError("CONFLICT", "Captured window evidence changed", 409)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _append(
        repo: UsageWindowRepository, context: UsageTraceConsumeContext, value: StoredUsageWindow
    ) -> None:
        session, action = repo.session, "lyra.usage.window_captured"
        payload = {
            "snapshot_id": str(value.request.id),
            "node_id": value.request.query.node_id,
            "fingerprint": value.fingerprint,
            "coverage": value.window.coverage,
            "ledger_revision": value.window.ledger.revision,
            "source_ceiling": value.window.source.highest_committed_seq,
        }
        session.add(
            DomainEvent(
                event_id=str(value.event_id),
                event_type=action,
                event_version=1,
                aggregate_type="usage_window_snapshot",
                aggregate_id=str(value.request.id),
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.id),
                payload=payload,
            )
        )
        session.flush()
        repo.append(value)
        session.add(OutboxEntry(event_id=str(value.event_id), created_at=value.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=str(value.request.id),
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
