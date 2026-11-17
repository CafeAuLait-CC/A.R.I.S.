import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import ExternalSubject, ExternalUserLookup
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.notifications.models import NotificationOutbox
from aris.core.notifications.repo import NotificationRepository
from aris.core.notifications.schemas import (
    NoticeContent,
    NotificationHandle,
    NotificationIntent,
    NotificationTemplateDefinition,
)
from aris.core.time import utc


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class NotificationTemplate(ABC):
    """Trusted pure renderer; no external I/O or database access."""

    definition: NotificationTemplateDefinition

    @abstractmethod
    def render(self, parameters: dict[str, JsonValue]) -> NoticeContent:
        """Validate typed input and return a bounded presentation snapshot."""


class NotificationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        namespace: IdentityNamespace,
        templates: Sequence[NotificationTemplate],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.namespace = sessions, namespace
        self.access = AccessService(sessions)
        self.clock = clock or (lambda: datetime.now(UTC))
        if len(templates) > 64:
            raise ValueError("notification template budget exceeded")
        self.templates: dict[str, tuple[NotificationTemplateDefinition, NotificationTemplate]] = {}
        for template in templates:
            definition = NotificationTemplateDefinition.model_validate(
                template.definition.model_dump()
            )
            if definition.template_id in self.templates:
                raise ValueError("duplicate notification template")
            self.templates[definition.template_id] = definition, template

    def now(self) -> datetime:
        return utc(self.clock())

    def target(self, session: Session, recipient_user_id: str) -> ExternalSubject:
        target = self.access.external_target_in_transaction(
            session,
            ExternalUserLookup(
                user_id=recipient_user_id,
                provider=self.namespace.provider,
                namespace=self.namespace.namespace,
            ),
        )
        if target is None:
            raise DomainError(
                "RECIPIENT_UNAVAILABLE", "Notification recipient is not registered", 409
            )
        return target.subject

    def audit(
        self,
        session: Session,
        record: NotificationOutbox,
        action: str,
        code: str | None = None,
        *,
        attempt_id: str | None = None,
        attempt: int | None = None,
    ) -> None:
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor="notification-service",
                action=action,
                target=record.id,
                request_id=record.request_id,
                recorded_at=self.now(),
                changes={
                    "event_id": record.event_id,
                    "attempt": record.attempts if attempt is None else attempt,
                    "attempt_id": attempt_id or record.last_attempt_id,
                    "code": code,
                },
            )
        )

    @staticmethod
    def record_fingerprint(record: NotificationOutbox) -> str:
        return fingerprint(
            {
                "event_id": record.event_id,
                "recipient_user_id": record.recipient_user_id,
                "target": {
                    "provider": record.provider,
                    "namespace": record.namespace,
                    "external_id": record.external_id,
                },
                "template_id": record.template_id,
                "template_fingerprint": record.template_fingerprint,
                "content": record.content,
            }
        )

    def validate_snapshot(self, session: Session, record: NotificationOutbox) -> NoticeContent:
        try:
            if self.record_fingerprint(record) != record.fingerprint:
                raise DomainError("NOTIFICATION_CHANGED", "Notification snapshot differs", 409)
            content = NoticeContent.model_validate(record.content)
            registration = self.templates.get(record.template_id)
            if (
                registration is None
                or fingerprint(registration[0].model_dump()) != record.template_fingerprint
            ):
                raise DomainError("TEMPLATE_CHANGED", "Notification template differs", 409)
            current = self.target(session, record.recipient_user_id)
            if (current.provider, current.namespace, current.external_id) != (
                record.provider,
                record.namespace,
                record.external_id,
            ):
                raise DomainError(
                    "RECIPIENT_CHANGED", "Notification recipient binding changed", 409
                )
            return content
        except (ValueError, TypeError, OverflowError):
            raise DomainError(
                "NOTIFICATION_INVALID", "Notification snapshot is invalid", 409
            ) from None

    def enqueue(self, session: Session, intent: NotificationIntent) -> NotificationHandle:
        registration = self.templates.get(intent.template_id)
        if registration is None:
            raise DomainError(
                "TEMPLATE_UNAVAILABLE", "Notification template is not registered", 503
            )
        definition, template = registration
        try:
            encoded = json.dumps(intent.parameters, allow_nan=False).encode()
            if len(encoded) > 16384:
                raise ValueError("notification input budget exceeded")
            content = NoticeContent.model_validate(template.render(intent.parameters).model_dump())
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise DomainError(
                "VALIDATION_ERROR", "Notification template input is invalid", 422
            ) from None
        with transaction_savepoint(session):
            target = self.target(session, str(intent.recipient_user_id))
            now = self.now()
            values = {
                "id": str(uuid4()),
                "event_id": str(intent.event_id),
                "recipient_user_id": str(intent.recipient_user_id),
                **target.model_dump(),
                "template_id": intent.template_id,
                "template_fingerprint": fingerprint(definition.model_dump()),
                "content": content.model_dump(mode="json"),
                "dedupe_sha256": fingerprint(
                    [str(intent.event_id), str(intent.recipient_user_id), intent.template_id]
                ),
                "state": "queued",
                "request_id": intent.request_id,
                "attempts": 0,
                "last_attempt_id": None,
                "available_at": now,
                "lease_token": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "created_at": now,
                "completed_at": None,
            }
            record = NotificationOutbox(**values)
            values["fingerprint"] = self.record_fingerprint(record)
            existing, created = NotificationRepository(session).add_or_existing(values)
            if existing.fingerprint != values["fingerprint"]:
                raise DomainError("CONFLICT", "Notification identity has different content", 409)
            if created:
                self.audit(session, existing, "notification.queued")
            return NotificationHandle(
                notification_id=existing.id, state=existing.state, duplicate=not created
            )
