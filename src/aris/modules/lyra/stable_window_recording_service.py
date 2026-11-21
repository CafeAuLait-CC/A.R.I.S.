"""Record accepted open/closed windows atomically through the existing ledger authority."""

from time import monotonic

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.modules.lyra.derivation_schemas import UsageDerivationContext, UsageDerivationRequest
from aris.modules.lyra.derivation_service import UsageDerivationService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import (
    StoredUsageOperation,
    UsagePostingContext,
    UsagePostingRequest,
)
from aris.modules.lyra.ledger_service import UsageLedgerService
from aris.modules.lyra.stable_window_acceptance_schemas import (
    AcceptStableWindow,
    StoredAcceptedStableWindow,
)
from aris.modules.lyra.stable_window_acceptance_service import StableWindowAcceptanceService
from aris.modules.lyra.stable_window_recording_schemas import (
    RecordAcceptedStableWindow,
    StableWindowRecordingReceipt,
)


class StableWindowRecordingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        acceptances: StableWindowAcceptanceService,
        derivations: UsageDerivationService,
        ledger: UsageLedgerService,
        *,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("stable recording requires a bounded internal timeout")
        self.sessions, self.acceptances, self.derivations, self.ledger = (
            sessions,
            acceptances,
            derivations,
            ledger,
        )
        self.timeout_ms = timeout_ms

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(database_failure(error), "Stable recording database is unavailable", 503)

    def _configuration(self, accepted: StoredAcceptedStableWindow) -> None:
        if self.ledger.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Stable recording is not configured", 503)
        if self.ledger.configuration != accepted.configuration:
            raise DomainError("CONFLICT", "Stable recording configuration differs", 409)

    @staticmethod
    def _derivation(
        accepted: StoredAcceptedStableWindow, request: RecordAcceptedStableWindow
    ) -> UsageDerivationRequest:
        original = accepted.proof.original
        return UsageDerivationRequest(
            id=request.derivation_id,
            sources=original.sources,
            as_of=accepted.request.window.reference.source_cutoff,
            identity_revision=accepted.request.identity_revision,
            continuity=original.continuity,
            formation=original.formation,
        )

    def _posting(
        self,
        session: Session,
        accepted: StoredAcceptedStableWindow,
        request: RecordAcceptedStableWindow,
    ) -> UsagePostingRequest:
        stored = self.derivations.exact_in_transaction(session, request.derivation_id)
        artifact = stored.artifact
        attributed = artifact.analysis.request.attribution
        matches = [
            index
            for index, candidate in enumerate(attributed.residency.sessions)
            if candidate == accepted.proof.original.candidate
        ]
        if artifact.request != self._derivation(accepted, request) or len(matches) != 1:
            raise DomainError("CONFLICT", "Stable derivation differs from first evidence", 409)
        index = matches[0]
        identity = attributed.attributions[index].identity
        if identity.status != "resolved" or identity.user_id != accepted.parent_identity.user_id:
            raise DomainError("USAGE_NEEDS_REVIEW", "Stable owner differs from entire parent", 409)
        return UsagePostingRequest(
            id=request.operation_id,
            derivation_id=request.derivation_id,
            session_index=index,
            expected_result_fingerprint=stored.receipt.result_fingerprint,
            expected_configuration_fingerprint=accepted.configuration.fingerprint,
        )

    def _receipt(
        self,
        session: Session,
        accepted: StoredAcceptedStableWindow,
        request: RecordAcceptedStableWindow,
        operation: StoredUsageOperation,
        *,
        duplicate: bool,
    ) -> StableWindowRecordingReceipt:
        expected = self._posting(session, accepted, request)
        if operation.request != expected or operation.receipt.head.node_id != (
            accepted.proof.original.parent_run.first.node_id
        ):
            raise DomainError("CONFLICT", "Stable ledger operation has different evidence", 409)
        return StableWindowRecordingReceipt(
            request=request,
            acceptance=accepted.receipt(duplicate=False),
            derivation_result_fingerprint=expected.expected_result_fingerprint,
            session_index=expected.session_index,
            ledger=operation.receipt.model_copy(update={"duplicate": False}),
            duplicate=duplicate,
        )

    def accept_and_record(
        self, context: UsagePostingContext, request: AcceptStableWindow
    ) -> StableWindowRecordingReceipt:
        """New acceptance, derivation, measurement and their events commit together."""
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.accept_and_record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def accept_and_record_in_transaction(
        self, session: Session, context: UsagePostingContext, request: AcceptStableWindow
    ) -> StableWindowRecordingReceipt:
        deadline = monotonic() + self.timeout_ms / 1000
        with transaction_savepoint(session), session.no_autoflush:
            accepted = self.acceptances.accept_in_transaction(session, context, request)
            result = self.record_in_transaction(
                session,
                context,
                RecordAcceptedStableWindow(
                    key=accepted.key, expected_acceptance_fingerprint=accepted.fingerprint
                ),
            )
            if monotonic() >= deadline:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR",
                    "Stable acceptance and recording budget exceeded",
                    503,
                )
            return result

    def record(
        self, context: UsagePostingContext, request: RecordAcceptedStableWindow
    ) -> StableWindowRecordingReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def record_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordAcceptedStableWindow
    ) -> StableWindowRecordingReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = RecordAcceptedStableWindow.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                accepted = self.acceptances.exact_in_transaction(session, request.key)
                if accepted.fingerprint != request.expected_acceptance_fingerprint:
                    raise DomainError("CONFLICT", "First stable acceptance differs", 409)
                repo = UsageLedgerRepository(session)
                repo.lock("operation", str(request.operation_id))
                prior = repo.operation(request.operation_id)
                if prior is not None:
                    # Historical success remains historical after pause, source retention
                    # or a reversal. This path never creates or resurrects a measurement.
                    return self._receipt(session, accepted, request, prior, duplicate=True)
                self._configuration(accepted)
                self.acceptances.verified_in_transaction(session, request.key)
                self.derivations.record_in_transaction(
                    session,
                    UsageDerivationContext(request_id=context.request_id),
                    self._derivation(accepted, request),
                )
                posting = self._posting(session, accepted, request)
                self.ledger.post_in_transaction(session, context, posting)
                if self.acceptances.verified_in_transaction(session, request.key) != accepted:
                    raise DomainError(
                        "CONFLICT", "First stable evidence changed before commit", 409
                    )
                self._configuration(accepted)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Stable recording budget exceeded", 503
                    )
                result = repo.operation(request.operation_id)
                if result is None:
                    raise DomainError("DEPENDENCY_UNAVAILABLE", "Stable recording unavailable", 503)
                return self._receipt(session, accepted, request, result, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable recording evidence is inconsistent", 503
            ) from None
