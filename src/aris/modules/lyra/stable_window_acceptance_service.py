"""Atomically retain the first qualified proof, without creating measured usage."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_schemas import (
    HistoricalNodeUserBatch,
    HistoricalNodeUserBatchResult,
    HistoricalNodeUserQuery,
    HistoricalNodeUserResolution,
)
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration, UsagePostingContext
from aris.modules.lyra.residency_attribution import HistoricalIdentityReader
from aris.modules.lyra.residency_layout_verification_schemas import (
    StableResidencyWindowReference,
    VerifiedStableResidencyWindow,
)
from aris.modules.lyra.stable_window_acceptance_repo import StableWindowAcceptanceRepository
from aris.modules.lyra.stable_window_acceptance_schemas import (
    AcceptedStableWindowReceipt,
    AcceptStableWindow,
    StoredAcceptedStableWindow,
    accepted_window_fingerprint,
)


class StableWindowProofReader(Protocol):
    def verify_in_transaction(
        self, session: Session, request: StableResidencyWindowReference
    ) -> VerifiedStableResidencyWindow: ...


class StableWindowAcceptanceService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        evidence: StableWindowProofReader,
        identities: HistoricalIdentityReader,
        *,
        configuration: UsagePostingConfiguration | None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("stable acceptance requires a bounded internal timeout")
        self.sessions, self.evidence, self.identities = sessions, evidence, identities
        self.configuration = configuration
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stable acceptance requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Stable acceptance budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(
            database_failure(error), "Stable acceptance database is unavailable", 503
        )

    def _configuration(self, request: AcceptStableWindow) -> UsagePostingConfiguration:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Stable acceptance is not configured", 503)
        value = UsagePostingConfiguration.model_validate(self.configuration.model_dump(mode="json"))
        if value.fingerprint != request.expected_recording_configuration_fingerprint:
            raise DomainError("CONFLICT", "Stable acceptance configuration differs", 409)
        return value

    def _proof(
        self, session: Session, request: AcceptStableWindow
    ) -> VerifiedStableResidencyWindow:
        proof = self.evidence.verify_in_transaction(session, request.window)
        proof = VerifiedStableResidencyWindow.model_validate(proof.model_dump(mode="json"))
        if proof.request != request.window or proof.original.read_at > self._now():
            raise DomainError("CONFLICT", "Stable acceptance source binding differs", 409)
        return proof

    def _identity(
        self, session: Session, request: AcceptStableWindow, proof: VerifiedStableResidencyWindow
    ) -> HistoricalNodeUserResolution:
        run = proof.original.parent_run
        query = HistoricalNodeUserQuery(
            node_id=run.first.node_id,
            os_username=run.username,
            start_at=run.first.compute_observed_at,
            end_at=run.last.compute_observed_at,
            as_of=request.window.reference.source_cutoff,
            through_revision=request.identity_revision,
        )
        result = self.identities.resolve_many_in_transaction(
            session, HistoricalNodeUserBatch(queries=(query,))
        )
        result = HistoricalNodeUserBatchResult.model_validate(result.model_dump(mode="json"))
        if (
            len(result.answers) != 1
            or result.answers[0].query != query
            or result.directory_revision != request.identity_revision
        ):
            raise DomainError("CONFLICT", "Stable parent identity snapshot differs", 409)
        identity = result.answers[0]
        if identity.status != "resolved":
            raise DomainError(
                "USAGE_NEEDS_REVIEW", "Entire stable parent identity is unresolved", 409
            )
        return identity

    def exact_in_transaction(self, session: Session, key: str) -> StoredAcceptedStableWindow:
        """Read the historical first acceptance; no claim that raw sources remain available."""
        if re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise DomainError("VALIDATION_ERROR", "Invalid accepted window key", 422)
        with session.no_autoflush:
            value = StableWindowAcceptanceRepository(session).get(key)
        if value is None:
            raise DomainError("NOT_FOUND", "Accepted window not found", 404)
        return value

    def exact(self, key: str) -> StoredAcceptedStableWindow:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.exact_in_transaction(session, key)
        except DBAPIError as error:
            raise self._failure(error) from None

    def accept(
        self, context: UsagePostingContext, request: AcceptStableWindow
    ) -> AcceptedStableWindowReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.accept_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def accept_in_transaction(
        self, session: Session, context: UsagePostingContext, request: AcceptStableWindow
    ) -> AcceptedStableWindowReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = AcceptStableWindow.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                proof = self._proof(session, request)
                repo = StableWindowAcceptanceRepository(session)
                repo.lock(proof.layout_key)
                self._deadline(deadline)
                prior = repo.get(proof.window.key)
                if prior is not None:
                    if (
                        request.expected_recording_configuration_fingerprint
                        != prior.configuration.fingerprint
                        or proof.layout_key != prior.proof.layout_key
                        or proof.method != prior.proof.method
                        or proof.window.index != prior.proof.window.index
                        or proof.window.kind != prior.proof.window.kind
                        or proof.original.candidate != prior.proof.original.candidate
                    ):
                        raise DomainError("CONFLICT", "Accepted window has different evidence", 409)
                    return prior.receipt(duplicate=True)
                config = self._configuration(request)
                original = proof.original
                if (
                    original.parent_candidate_state != "stable_candidate"
                    or original.candidate.state != "stable_candidate"
                ):
                    raise DomainError("USAGE_NEEDS_REVIEW", "Window has insufficient evidence", 409)
                if original.parent_run.rule_fingerprint != config.timing_fingerprint:
                    raise DomainError("CONFLICT", "Stable parent timing differs", 409)
                if original.parent_run.first.compute_observed_at < config.recording_start_at:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Stable parent precedes recording start", 409
                    )
                identity = self._identity(session, request, proof)
                draft = StoredAcceptedStableWindow.model_construct(
                    request=request,
                    proof=proof,
                    configuration=config,
                    parent_identity=identity,
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                value = StoredAcceptedStableWindow.model_validate(
                    {**draft.model_dump(), "fingerprint": accepted_window_fingerprint(draft)}
                )
                self._emit(session, context, value)
                repo.append(value)
                session.flush()
                current = self._proof(session, request)
                if (
                    current.fingerprint != proof.fingerprint
                    or self._identity(session, request, current) != identity
                    or self._configuration(request) != config
                ):
                    raise DomainError("CONFLICT", "Stable evidence changed before commit", 409)
                self._deadline(deadline)
                if repo.get(proof.window.key) != value:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Accepted window is unavailable", 503
                    )
                return value.receipt(duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable acceptance is inconsistent", 503
            ) from None

    @staticmethod
    def _emit(
        session: Session, context: UsagePostingContext, value: StoredAcceptedStableWindow
    ) -> None:
        action = "lyra.usage.stable_window_accepted"
        event_id = str(value.event_id)
        payload = {
            "window_key": value.proof.window.key,
            "node_id": value.proof.original.parent_run.first.node_id,
            "run_id": value.proof.original.parent_run.id,
            "fingerprint": value.fingerprint,
            "formal_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="accepted_stable_window",
                aggregate_id=value.proof.window.key,
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.window.reference.record_id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=value.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=value.proof.window.key,
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
