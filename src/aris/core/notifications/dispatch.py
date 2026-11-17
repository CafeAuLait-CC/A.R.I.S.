from datetime import timedelta
from typing import Literal, Protocol
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.commands.budgets import transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.notifications.administration import MANUAL_RETRY
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.repo import NotificationRepository
from aris.core.notifications.schemas import (
    NotificationClaim,
    NotificationDeliveryError,
    NotificationLimits,
    NotificationResult,
    PreparedNotification,
    TransportReceipt,
)
from aris.core.notifications.service import NotificationService
from aris.core.time import utc


class NotificationTransport(Protocol):
    def send(self, notification: PreparedNotification) -> TransportReceipt: ...


class NotificationDispatcher:
    def __init__(
        self,
        service: NotificationService,
        limits: NotificationLimits,
        transport: NotificationTransport,
    ) -> None:
        self.service, self.sessions, self.limits, self.transport = (
            service,
            service.sessions,
            limits,
            transport,
        )

    def _budget(self, session: Session) -> None:
        transaction_budget(
            session,
            self.limits.lock_timeout_ms,
            self.limits.statement_timeout_ms,
            self.limits.transaction_timeout_ms,
        )

    def _terminal(
        self,
        session: Session,
        record: NotificationOutbox,
        state: Literal["delivered", "failed", "uncertain"],
        code: str | None = None,
    ) -> NotificationResult:
        record.state, record.completed_at, record.last_error_code = state, self.service.now(), code
        record.lease_token, record.lease_expires_at = None, None
        self.service.audit(session, record, "notification." + state, code)
        return NotificationResult(notification_id=record.id, state=state, code=code)

    @staticmethod
    def _lost(claim: NotificationClaim) -> NotificationResult:
        return NotificationResult(notification_id=claim.notification_id, state="lost_claim")

    def _eligible(self, record: NotificationOutbox) -> bool:
        return (
            record.provider == self.service.namespace.provider
            and record.namespace == self.service.namespace.namespace
            and record.template_id in self.service.templates
        )

    def claim(self) -> NotificationClaim | NotificationResult | None:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = NotificationRepository(session)
                record = repo.next_notification(
                    self.service.now(),
                    namespace=self.service.namespace,
                    template_ids=tuple(self.service.templates),
                )
                if record is None:
                    return None
                if record.state == "running":
                    previous = repo.attempt(record.last_attempt_id or "")
                    if previous is None:
                        return self._terminal(session, record, "failed", "ATTEMPT_MISSING")
                    if previous.state == "sending":
                        previous.state, previous.finished_at = "uncertain", self.service.now()
                        previous.error_code = "INTERRUPTED_DELIVERY"
                        return self._terminal(session, record, "uncertain", "INTERRUPTED_DELIVERY")
                    if previous.state != "claimed":
                        return self._terminal(session, record, "failed", "ATTEMPT_INVALID")
                    previous.state, previous.finished_at = "abandoned", self.service.now()
                    previous.error_code = "LEASE_EXPIRED_BEFORE_SEND"
                    self.service.audit(
                        session, record, "notification.attempt.abandoned", previous.error_code
                    )
                if record.attempts >= self.limits.max_attempts:
                    return self._terminal(session, record, "failed", "ATTEMPTS_EXHAUSTED")
                now = self.service.now()
                record.attempts += 1
                record.state, record.lease_token = "running", str(uuid4())
                record.last_attempt_id = record.lease_token
                record.lease_expires_at = now + timedelta(seconds=self.limits.lease_seconds)
                session.add(
                    NotificationAttempt(
                        id=record.lease_token,
                        notification_id=record.id,
                        attempt=record.attempts,
                        state="claimed",
                        started_at=now,
                        prepared_at=None,
                        finished_at=None,
                        error_code=None,
                        channel_ref=None,
                        message_ref=None,
                    )
                )
                self.service.audit(session, record, "notification.claimed")
                return NotificationClaim(
                    notification_id=record.id,
                    attempt_id=record.lease_token,
                    attempt=record.attempts,
                    lease_expires_at=record.lease_expires_at,
                )
        except DBAPIError:
            raise DomainError(
                "NOTIFICATION_UNAVAILABLE", "Claim outcome unavailable", 503
            ) from None

    def _owned(self, record: NotificationOutbox, claim: NotificationClaim) -> bool:
        return (
            record.state == "running"
            and record.lease_token == str(claim.attempt_id)
            and record.last_attempt_id == str(claim.attempt_id)
            and record.attempts == claim.attempt
            and record.lease_expires_at is not None
            and utc(record.lease_expires_at) == utc(claim.lease_expires_at)
            and utc(record.lease_expires_at) > self.service.now()
        )

    def prepare(self, claim: NotificationClaim) -> PreparedNotification | NotificationResult:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = NotificationRepository(session)
                record = repo.notification(str(claim.notification_id), lock=True)
                if record is None or not self._eligible(record) or not self._owned(record, claim):
                    return self._lost(claim)
                attempt = repo.attempt(str(claim.attempt_id))
                if (
                    attempt is None
                    or attempt.notification_id != record.id
                    or attempt.state != "claimed"
                ):
                    return self._lost(claim)
                try:
                    content = self.service.validate_snapshot(session, record)
                except DomainError as error:
                    attempt.state, attempt.finished_at, attempt.error_code = (
                        "rejected",
                        self.service.now(),
                        error.code,
                    )
                    return self._terminal(session, record, "failed", error.code)
                if not self._owned(record, claim):
                    return self._lost(claim)
                attempt.state, attempt.prepared_at = "sending", self.service.now()
                self.service.audit(session, record, "notification.sending")
                prepared = PreparedNotification(
                    notification_id=record.id,
                    attempt_id=attempt.id,
                    target={
                        "provider": record.provider,
                        "namespace": record.namespace,
                        "external_id": record.external_id,
                    },
                    content=content,
                )
                commit_owned_transaction(session)
                return prepared
        except DBAPIError:
            raise DomainError(
                "NOTIFICATION_UNAVAILABLE", "Preparation outcome unavailable", 503
            ) from None

    def finish(
        self,
        claim: NotificationClaim,
        *,
        receipt: TransportReceipt | None = None,
        error: NotificationDeliveryError | None = None,
    ) -> NotificationResult:
        if (receipt is None) == (error is None):
            raise ValueError("provide exactly one receipt or error")
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = NotificationRepository(session)
                record = repo.notification(str(claim.notification_id), lock=True)
                attempt = repo.attempt(str(claim.attempt_id))
                if (
                    record is None
                    or not self._eligible(record)
                    or attempt is None
                    or attempt.notification_id != record.id
                    or attempt.attempt != claim.attempt
                ):
                    return self._lost(claim)
                if attempt.state in {"delivered", "late_delivered"}:
                    if receipt is not None and (attempt.channel_ref, attempt.message_ref) != (
                        receipt.channel_ref,
                        receipt.message_ref,
                    ):
                        raise DomainError("CONFLICT", "Stored delivery receipt differs", 409)
                    return NotificationResult(
                        notification_id=record.id,
                        state="delivered" if record.state == "delivered" else "lost_claim",
                    )
                if attempt.state not in {"sending", "uncertain"}:
                    if record.state == "queued" and record.last_attempt_id == attempt.id:
                        return NotificationResult(
                            notification_id=record.id, state="retry", code=attempt.error_code
                        )
                    if record.state == "failed" and record.last_attempt_id == attempt.id:
                        return NotificationResult(
                            notification_id=record.id, state="failed", code=attempt.error_code
                        )
                    return self._lost(claim)
                current = (
                    record.last_attempt_id == attempt.id
                    and record.attempts == claim.attempt
                    and (
                        record.state in {"running", "uncertain"}
                        or (
                            receipt is not None
                            and record.state == "queued"
                            and record.last_error_code == MANUAL_RETRY
                            and attempt.state == "uncertain"
                        )
                    )
                )
                if (
                    not current
                    and record.state == "queued"
                    and record.last_attempt_id == attempt.id
                    and record.last_error_code == MANUAL_RETRY
                    and attempt.state == "uncertain"
                    and error is not None
                ):
                    if not error.outcome_unknown:
                        attempt.state, attempt.finished_at, attempt.error_code = (
                            "rejected",
                            self.service.now(),
                            error.code,
                        )
                        self.service.audit(
                            session,
                            record,
                            "notification.attempt.late_rejected",
                            error.code,
                            attempt_id=attempt.id,
                            attempt=attempt.attempt,
                        )
                        commit_owned_transaction(session)
                    return self._lost(claim)
                if (
                    current
                    and record.state == "uncertain"
                    and attempt.state == "uncertain"
                    and error is not None
                    and error.outcome_unknown
                    and error.code == attempt.error_code
                ):
                    return NotificationResult(
                        notification_id=record.id, state="uncertain", code=attempt.error_code
                    )
                now = self.service.now()
                if receipt is not None:
                    attempt.state = "delivered" if current else "late_delivered"
                    attempt.finished_at, attempt.error_code = now, None
                    attempt.channel_ref, attempt.message_ref = (
                        receipt.channel_ref,
                        receipt.message_ref,
                    )
                    if current:
                        result = self._terminal(session, record, "delivered")
                    else:
                        self.service.audit(
                            session,
                            record,
                            "notification.attempt.late_delivered",
                            attempt_id=attempt.id,
                            attempt=attempt.attempt,
                        )
                        result = self._lost(claim)
                else:
                    assert error is not None
                    attempt.state = "uncertain" if error.outcome_unknown else "rejected"
                    attempt.finished_at, attempt.error_code = now, error.code
                    if not current:
                        result = self._lost(claim)
                    elif error.outcome_unknown:
                        result = self._terminal(session, record, "uncertain", error.code)
                    elif error.retryable and record.attempts < self.limits.max_attempts:
                        delay = max(
                            error.retry_after,
                            min(60, self.limits.retry_base_seconds * 2 ** (record.attempts - 1)),
                        )
                        record.state, record.available_at = "queued", now + timedelta(seconds=delay)
                        record.lease_token, record.lease_expires_at, record.completed_at = (
                            None,
                            None,
                            None,
                        )
                        record.last_error_code = error.code
                        self.service.audit(session, record, "notification.retry", error.code)
                        result = NotificationResult(
                            notification_id=record.id, state="retry", code=error.code
                        )
                    else:
                        result = self._terminal(session, record, "failed", error.code)
                commit_owned_transaction(session)
                return result
        except DBAPIError:
            raise DomainError(
                "NOTIFICATION_UNAVAILABLE", "Delivery receipt outcome unavailable", 503
            ) from None

    def run_once(self) -> NotificationResult | None:
        claim = self.claim()
        if not isinstance(claim, NotificationClaim):
            return claim
        prepared = self.prepare(claim)
        if isinstance(prepared, NotificationResult):
            return prepared
        if self.service.now() >= utc(claim.lease_expires_at):
            return self.finish(
                claim, error=NotificationDeliveryError("LEASE_EXPIRED_BEFORE_SEND", retryable=True)
            )
        try:
            receipt = TransportReceipt.model_validate(self.transport.send(prepared).model_dump())
        except NotificationDeliveryError as error:
            return self.finish(claim, error=error)
        except Exception:
            return self.finish(
                claim, error=NotificationDeliveryError("DELIVERY_UNCERTAIN", outcome_unknown=True)
            )
        return self.finish(claim, receipt=receipt)
