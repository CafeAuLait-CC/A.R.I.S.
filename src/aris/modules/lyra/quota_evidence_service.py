"""Authorized fixed-snapshot model totals; no current usage or quota writes."""

from collections import defaultdict
from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.quota_evidence_schemas import (
    HistoricalModelUsage,
    HistoricalQuotaEvidence,
    QuotaEvidenceSelection,
    quota_evidence_fingerprint,
)
from aris.modules.lyra.recorded_usage import RecordedUsageQuery, recorded_usage_metrics
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow


class HistoricalQuotaEvidenceService:
    def __init__(self, sessions: sessionmaker[Session], *, timeout_ms: int = 3000) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise ValueError("historical quota evidence requires a bounded timeout")
        self.sessions, self.timeout_ms = sessions, timeout_ms
        self.access = AccessService(sessions)

    def _budget(self, session: Session) -> None:
        transaction_budget(session, 100, 1000, self.timeout_ms)

    @staticmethod
    def _permission(actor: ActorContext, selection: QuotaEvidenceSelection) -> AccessRequest:
        return AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_own"
            if selection.owner_id == actor.actor_id
            else "usage.read_all",
            scope=AccessScope(type="node", id=selection.node_id),
        )

    def require_access(self, actor: ActorContext, selection: QuotaEvidenceSelection) -> None:
        """Recheck current access after a consuming domain finishes its own computation."""
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        selection = QuotaEvidenceSelection.model_validate(selection.model_dump(mode="json"))
        try:
            with self.sessions() as session, session.no_autoflush:
                self._budget(session)
                self.access.require_in_transaction(session, self._permission(actor, selection))
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Historical usage evidence database is unavailable",
            503,
        )

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Quota evidence budget exceeded", 503)

    def read(
        self, actor: ActorContext, selection: QuotaEvidenceSelection
    ) -> HistoricalQuotaEvidence:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        selection = QuotaEvidenceSelection.model_validate(selection.model_dump(mode="json"))
        permission = self._permission(actor, selection)
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as session, session.no_autoflush:
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                elif dialect == "sqlite":
                    session.execute(text("BEGIN"))
                else:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Unsupported evidence database", 503
                    )
                self._budget(session)
                self.access.require_in_transaction(session, permission)
                repo = UsageWindowRepository(session)
                scope = repo.scope(selection.snapshot_id)
                if (
                    scope is None
                    or scope[0] != selection.node_id
                    or scope[1] not in (None, selection.owner_id)
                ):
                    raise DomainError("NOT_FOUND", "Usage snapshot scope is unknown", 404)
                stored = repo.get(selection.snapshot_id)
                if stored is None or stored.fingerprint != selection.expected_snapshot_fingerprint:
                    raise DomainError("CONFLICT", "Usage snapshot fingerprint differs", 409)
                if (
                    stored.window.query.node_id != selection.node_id
                    or stored.window.query.owner_id not in (None, selection.owner_id)
                ):
                    raise DomainError("CONFLICT", "Usage snapshot scope differs", 409)
                result = self._build(session, selection, stored)
                self._deadline(deadline)
            # REPEATABLE READ intentionally fixes old facts, not old permissions.
            with self.sessions() as session, session.no_autoflush:
                self._budget(session)
                self.access.require_in_transaction(session, permission)
                self._deadline(deadline)
            return result
        except DBAPIError as error:
            raise self._failure(error) from None
        except ValueError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Historical usage evidence is inconsistent", 503
            ) from None

    @staticmethod
    def _build(
        session: Session, selection: QuotaEvidenceSelection, stored: StoredUsageWindow
    ) -> HistoricalQuotaEvidence:
        window = stored.window
        complete = window.coverage == "complete"
        models: tuple[HistoricalModelUsage, ...] | None = None
        total = None
        if complete:
            entries = UsageLedgerRepository(session).prefix(window.ledger)
            recorded = recorded_usage_metrics(
                RecordedUsageQuery(
                    node_id=selection.node_id,
                    start_at=window.query.start_at,
                    end_at=window.query.end_at,
                    snapshot=window.ledger,
                ),
                owner_id=selection.owner_id,
                head=window.ledger,
                entries=entries,
                read_at=window.read_at,
            )
            total = sum(
                cell.microseconds
                for cell in window.cells
                if cell.state == "recorded" and cell.owner_id == selection.owner_id
            )
            by_model: dict[str, int] = defaultdict(int)
            for item in recorded.contributions:
                if (
                    item.timing_fingerprint != window.configuration.recording.timing_fingerprint
                    or item.configuration_fingerprint != window.configuration.recording.fingerprint
                ):
                    raise DomainError("CONFLICT", "Usage model timing configuration differs", 409)
                by_model[item.model] += item.recorded_microseconds
            if recorded.recorded_microseconds != total:
                raise DomainError("CONFLICT", "Usage model totals differ from verified cells", 409)
            models = tuple(
                HistoricalModelUsage(model=model, observed_microseconds=duration)
                for model, duration in sorted(by_model.items())
            )
        draft = HistoricalQuotaEvidence.model_construct(
            selection=selection,
            window=window.query.model_copy(update={"owner_id": selection.owner_id}),
            captured_at=window.read_at,
            created_at=stored.created_at,
            source_fingerprint=window.source.fingerprint,
            source_ceiling=window.source.highest_committed_seq,
            gpu_count=len(window.configuration.gpu_uuids),
            ledger=window.ledger,
            configuration_fingerprint=window.configuration.fingerprint,
            timing_fingerprint=window.configuration.recording.timing_fingerprint,
            recording_start_at=window.configuration.recording.recording_start_at,
            identity_revision=window.identity_revision,
            coverage=window.coverage,
            source_prefix_complete=window.source_prefix_complete,
            total_usage_microseconds=total,
            models=models,
            snapshot_window_fingerprint=window.fingerprint,
            fingerprint="0" * 64,
        )
        return HistoricalQuotaEvidence.model_validate(
            {**draft.model_dump(mode="json"), "fingerprint": quota_evidence_fingerprint(draft)}
        )
