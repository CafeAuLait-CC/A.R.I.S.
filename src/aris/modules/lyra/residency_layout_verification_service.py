"""Resolve server-selected stable windows in one bounded read-only transaction."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod, plan_residency_windows
from aris.modules.lyra.residency_layout_verification_schemas import (
    StableResidencyWindowReference,
    VerifiedStableResidencyWindow,
    stable_window_fingerprint,
)
from aris.modules.lyra.residency_observed_window_schemas import (
    ResidencyObservedWindowReference,
    VerifiedResidencyObservedWindow,
)


class ObservedResidencyWindowReader(Protocol):
    def observed_window_in_transaction(
        self, session: Session, request: ResidencyObservedWindowReference
    ) -> VerifiedResidencyObservedWindow: ...


class StableResidencyWindowService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        originals: ObservedResidencyWindowReader,
        *,
        method: StableResidencyLayoutMethod,
        timeout_ms: int = 10000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise ValueError("stable window verification requires a bounded timeout")
        self.sessions, self.originals = sessions, originals
        self.method = StableResidencyLayoutMethod.model_validate(method.model_dump(mode="json"))
        self.timeout_ms = timeout_ms
        self.clock = clock or (lambda: datetime.now(UTC))

    def _deadline(self, deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Stable window read budget exceeded", 503)

    def _original(
        self, session: Session, request: ResidencyObservedWindowReference
    ) -> VerifiedResidencyObservedWindow:
        value = self.originals.observed_window_in_transaction(session, request)
        value = VerifiedResidencyObservedWindow.model_validate(value.model_dump(mode="json"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("stable window clock requires a timezone")
        if value.request != request or value.read_at > now:
            raise DomainError("CONFLICT", "Original window binding or read clock differs", 409)
        return value

    def verify(self, request: StableResidencyWindowReference) -> VerifiedStableResidencyWindow:
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.verify_in_transaction(session, request)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Stable window database is unavailable",
                503,
            ) from None

    def verify_in_transaction(
        self, session: Session, request: StableResidencyWindowReference
    ) -> VerifiedStableResidencyWindow:
        request = StableResidencyWindowReference.model_validate(request.model_dump(mode="json"))
        method = StableResidencyLayoutMethod.model_validate(self.method.model_dump(mode="json"))
        if request.expected_method_fingerprint != method.fingerprint:
            raise DomainError("CONFLICT", "Stable window method differs", 409)
        deadline = monotonic() + self.timeout_ms / 1000
        with session.no_autoflush:
            anchor = ResidencyObservedWindowReference(
                reference=request.reference,
                attestation_id=request.attestation_id,
                expected_attestation_fingerprint=request.expected_attestation_fingerprint,
                expected_trace_configuration_fingerprint=request.expected_trace_configuration_fingerprint,
                after_interval=0,
                through_interval=1,
            )
            first = self._original(session, anchor)
            self._deadline(deadline)
            try:
                page = plan_residency_windows(
                    first, method, first_window=request.window_index, limit=1
                )
            except ValueError:
                raise DomainError("NOT_FOUND", "Window is outside this fixed layout", 404) from None
            if not page.windows:
                raise DomainError(
                    "NOT_FOUND", "Window is not yet planned in this fixed prefix", 404
                )
            selected = page.windows[0]
            original = (
                first if selected.original == anchor else self._original(session, selected.original)
            )
            if (
                original.parent_run != first.parent_run
                or original.state_at_record != first.state_at_record
                or original.consumer_id != first.consumer_id
                or original.consumer_revision != first.consumer_revision
                or original.record_created_at != first.record_created_at
                or original.continuity != first.continuity
                or original.formation != first.formation
            ):
                raise DomainError("CONFLICT", "Fixed parent differs between original reads", 409)
            self._deadline(deadline)
            if self.method != method:
                raise DomainError(
                    "CONFLICT", "Stable window method changed during verification", 409
                )
            draft = VerifiedStableResidencyWindow.model_construct(
                request=request,
                method=method,
                layout_key=page.layout_key,
                window=selected,
                original=original,
                fingerprint="0" * 64,
            )
            value = VerifiedStableResidencyWindow.model_validate(
                {**draft.model_dump(), "fingerprint": stable_window_fingerprint(draft)}
            )
            self._deadline(deadline)
            return value

    def layout_witness_in_transaction(
        self, session: Session, request: StableResidencyWindowReference
    ) -> VerifiedResidencyObservedWindow:
        """Verify a fixed parent anchor even when it has no dispatchable prefix yet."""
        request = StableResidencyWindowReference.model_validate(request.model_dump(mode="json"))
        method = StableResidencyLayoutMethod.model_validate(self.method.model_dump(mode="json"))
        if request.window_index != 0 or request.expected_method_fingerprint != method.fingerprint:
            raise DomainError("CONFLICT", "Stable layout witness method or origin differs", 409)
        deadline = monotonic() + self.timeout_ms / 1000
        with session.no_autoflush:
            result = self._original(
                session,
                ResidencyObservedWindowReference(
                    reference=request.reference,
                    attestation_id=request.attestation_id,
                    expected_attestation_fingerprint=request.expected_attestation_fingerprint,
                    expected_trace_configuration_fingerprint=request.expected_trace_configuration_fingerprint,
                    after_interval=0,
                    through_interval=1,
                ),
            )
            self._deadline(deadline)
            if self.method != method:
                raise DomainError("CONFLICT", "Stable layout method changed during read", 409)
            return result
