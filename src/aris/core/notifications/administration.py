from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.notifications.repo import NotificationRepository
from aris.core.notifications.schemas import NotificationLimits
from aris.core.notifications.service import NotificationService

MANAGE_NOTIFICATIONS = "notification.manage"
MANUAL_RETRY = "MANUAL_RETRY_APPROVED"


class NotificationAdminContext(AccessContract):
    actor_id: UUID
    operation_id: UUID
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class RetryUncertainNotification(AccessContract):
    notification_id: UUID
    expected_attempt_id: UUID
    reason: str = Field(min_length=1, max_length=256, repr=False)
    acknowledge_possible_duplicate: Literal[True]

    @field_validator("acknowledge_possible_duplicate", mode="before")
    @classmethod
    def explicit_acknowledgement(cls, value: object) -> object:
        if value is not True:
            raise ValueError(
                "an explicit acknowledgement of possible duplicate delivery is required"
            )
        return value

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a retry reason is required")
        return value.strip()


class NotificationRetryResult(AccessContract):
    notification_id: UUID
    state: Literal["queued"] = "queued"
    previous_attempt_id: UUID
    total_attempts: int = Field(ge=1)
    duplicate_delivery_possible: Literal[True] = True


class NotificationAdministrationService:
    """Authorize a further attempt; never performs I/O or asserts delivery."""

    def __init__(self, service: NotificationService, limits: NotificationLimits) -> None:
        self.service, self.sessions, self.limits = service, service.sessions, limits

    def _permission(self, session: Session, context: NotificationAdminContext) -> None:
        self.service.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=context.actor_id,
                permission=MANAGE_NOTIFICATIONS,
                scope=AccessScope(type="global", id="*"),
            ),
        )

    def retry(
        self, context: NotificationAdminContext, request: RetryUncertainNotification
    ) -> NotificationRetryResult:
        with self.sessions.begin() as session:
            transaction_budget(
                session,
                self.limits.lock_timeout_ms,
                self.limits.statement_timeout_ms,
                self.limits.transaction_timeout_ms,
            )
            return self.retry_in_transaction(session, context, request)

    def retry_in_transaction(
        self,
        session: Session,
        context: NotificationAdminContext,
        request: RetryUncertainNotification,
    ) -> NotificationRetryResult:
        with transaction_savepoint(session):
            self._permission(session, context)
            repo = NotificationRepository(session)
            record = repo.scoped_notification(
                str(request.notification_id),
                namespace=self.service.namespace,
                template_ids=tuple(self.service.templates),
                lock=True,
            )
            if record is None:
                raise DomainError("NOT_FOUND", "Notification is unavailable", 404)
            attempt = repo.attempt(str(request.expected_attempt_id))
            if (
                record.state != "uncertain"
                or record.last_attempt_id != str(request.expected_attempt_id)
                or attempt is None
                or attempt.notification_id != record.id
                or attempt.attempt != record.attempts
                or attempt.state != "uncertain"
            ):
                raise DomainError("CONFLICT", "Notification attempt or state changed", 409)
            if record.attempts >= self.limits.max_attempts:
                raise DomainError(
                    "RETRY_BUDGET_EXHAUSTED", "Notification attempt budget is exhausted", 409
                )
            if repo.has_successful_attempt(record.id):
                raise DomainError(
                    "ALREADY_DELIVERED", "A successful delivery is already verified", 409
                )
            self.service.validate_snapshot(session, record)
            self._permission(session, context)
            now = self.service.now()
            record.state, record.available_at = "queued", now
            record.completed_at, record.last_error_code = None, MANUAL_RETRY
            record.lease_token, record.lease_expires_at = None, None
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(context.actor_id),
                    action="notification.manual_retry.approved",
                    target=record.id,
                    request_id=context.request_id,
                    recorded_at=now,
                    changes={
                        "operation_id": str(context.operation_id),
                        "previous_attempt_id": attempt.id,
                        "total_attempts": record.attempts,
                        "reason": request.reason,
                        "duplicate_delivery_possible": True,
                    },
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="core.notification.retry_requested",
                    event_version=1,
                    aggregate_type="notification",
                    aggregate_id=record.id,
                    aggregate_version=record.attempts,
                    occurred_at=now,
                    recorded_at=now,
                    actor=str(context.actor_id),
                    correlation_id=context.request_id,
                    causation_id=str(context.operation_id),
                    payload={
                        "previous_attempt_id": attempt.id,
                        "duplicate_delivery_possible": True,
                    },
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            self._permission(session, context)
            return NotificationRetryResult(
                notification_id=record.id,
                previous_attempt_id=attempt.id,
                total_attempts=record.attempts,
            )
