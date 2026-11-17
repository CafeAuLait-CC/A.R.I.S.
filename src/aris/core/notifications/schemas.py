import re
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from aris.core.access.schemas import AccessContract, ExternalSubject


class NotificationTemplateDefinition(AccessContract):
    template_id: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    revision: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


class NoticeField(AccessContract):
    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class NoticeContent(AccessContract):
    title: str = Field(min_length=1, max_length=150)
    summary: str = Field(min_length=1, max_length=2000)
    fields: tuple[NoticeField, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def bounded_fallback(self) -> "NoticeContent":
        if len(self.fallback()) > 4000:
            raise ValueError("notification fallback budget exceeded")
        return self

    def fallback(self) -> str:
        return "\n".join(
            [self.title, self.summary, *(f"{item.label}: {item.value}" for item in self.fields)]
        )


class NotificationIntent(AccessContract):
    event_id: UUID
    recipient_user_id: UUID
    template_id: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    parameters: dict[str, JsonValue] = Field(repr=False)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class NotificationLimits(AccessContract):
    lock_timeout_ms: int = Field(gt=0, le=10000, strict=True)
    statement_timeout_ms: int = Field(gt=0, le=20000, strict=True)
    transaction_timeout_ms: int = Field(gt=0, le=30000, strict=True)
    lease_seconds: int = Field(ge=2, le=600, strict=True)
    max_attempts: int = Field(ge=1, le=20, strict=True)
    retry_base_seconds: int = Field(ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def ordered_timeouts(self) -> "NotificationLimits":
        if not (
            self.lock_timeout_ms
            < self.statement_timeout_ms
            < self.transaction_timeout_ms
            < self.lease_seconds * 1000
        ):
            raise ValueError("lease must exceed the ordered transaction budgets")
        return self


NotificationState = Literal["queued", "running", "delivered", "failed", "uncertain"]


class NotificationHandle(AccessContract):
    notification_id: UUID
    state: NotificationState
    duplicate: bool


class NotificationClaim(AccessContract):
    notification_id: UUID
    attempt_id: UUID
    attempt: int = Field(ge=1)
    lease_expires_at: AwareDatetime


class PreparedNotification(AccessContract):
    notification_id: UUID
    attempt_id: UUID
    target: ExternalSubject
    content: NoticeContent = Field(repr=False)


class TransportReceipt(AccessContract):
    channel_ref: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    message_ref: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")


class NotificationResult(AccessContract):
    notification_id: UUID
    state: Literal["delivered", "failed", "uncertain", "retry", "lost_claim"]
    code: str | None = None


class NotificationDeliveryError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        outcome_unknown: bool = False,
        retry_after: int = 0,
    ) -> None:
        self.code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) else "DELIVERY_ERROR"
        self.retryable, self.outcome_unknown = retryable, outcome_unknown
        self.retry_after = max(0, min(3600, retry_after))
        super().__init__(self.code)
