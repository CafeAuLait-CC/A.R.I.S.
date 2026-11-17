from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract, ExternalSubject
from aris.core.notifications.schemas import NoticeContent, NotificationState


class NotificationQueryLimits(AccessContract):
    lock_timeout_ms: int = Field(default=100, gt=0, le=1000, strict=True)
    statement_timeout_ms: int = Field(default=1000, gt=0, le=5000, strict=True)
    transaction_timeout_ms: int = Field(default=3000, gt=0, le=10000, strict=True)

    @model_validator(mode="after")
    def ordered_budgets(self) -> "NotificationQueryLimits":
        if not self.lock_timeout_ms < self.statement_timeout_ms < self.transaction_timeout_ms:
            raise ValueError("notification query budgets must increase")
        return self


class NotificationCursor(AccessContract):
    created_at: AwareDatetime
    notification_id: UUID


class UncertainNotificationsQuery(AccessContract):
    limit: int = Field(default=10, ge=1, le=20, strict=True)
    after: NotificationCursor | None = None


class NotificationAttemptView(AccessContract):
    attempt_id: UUID
    number: int = Field(ge=1, le=20)
    state: Literal[
        "claimed", "sending", "delivered", "rejected", "uncertain", "abandoned", "late_delivered"
    ]
    started_at: AwareDatetime
    prepared_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    channel_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^\S+$")
    message_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^\S+$")


class NotificationRetryEligibility(AccessContract):
    allowed: bool
    code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]{1,63}$")

    @model_validator(mode="after")
    def unambiguous(self) -> "NotificationRetryEligibility":
        if self.allowed == (self.code is not None):
            raise ValueError("retry eligibility must include a reason when blocked")
        return self


class NotificationOperatorView(AccessContract):
    notification_id: UUID
    recipient_user_id: UUID
    target: ExternalSubject
    template_id: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    created_at: AwareDatetime
    state: NotificationState
    total_attempts: int = Field(ge=0, le=20)
    maximum_attempts: int = Field(ge=1, le=20)
    last_attempt_id: UUID | None
    attempts: tuple[NotificationAttemptView, ...] = Field(max_length=20)
    known_successful_attempts: int = Field(ge=0, le=20)
    content: NoticeContent | None
    retry: NotificationRetryEligibility

    @model_validator(mode="after")
    def complete_evidence(self) -> "NotificationOperatorView":
        if tuple(item.number for item in self.attempts) != tuple(range(1, self.total_attempts + 1)):
            raise ValueError("all attempts must be present in order")
        if self.last_attempt_id != (self.attempts[-1].attempt_id if self.attempts else None):
            raise ValueError("last attempt evidence differs")
        if self.known_successful_attempts != sum(
            item.state in {"delivered", "late_delivered"} for item in self.attempts
        ):
            raise ValueError("successful attempt evidence differs")
        return self


class UncertainNotificationsPage(AccessContract):
    namespace: IdentityNamespace
    generated_at: AwareDatetime
    items: tuple[NotificationOperatorView, ...] = Field(max_length=20)
    next_cursor: NotificationCursor | None
