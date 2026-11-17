from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject
from aris.core.commands.budgets import transaction_budget
from aris.core.contracts import DomainError
from aris.core.notifications.administration import MANAGE_NOTIFICATIONS
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.query_schemas import (
    NotificationAttemptView,
    NotificationCursor,
    NotificationOperatorView,
    NotificationQueryLimits,
    NotificationRetryEligibility,
    UncertainNotificationsPage,
    UncertainNotificationsQuery,
)
from aris.core.notifications.repo import NotificationRepository
from aris.core.notifications.schemas import NoticeContent, NotificationLimits
from aris.core.notifications.service import NotificationService
from aris.core.time import utc


class NotificationOperatorQueries:
    def __init__(
        self,
        service: NotificationService,
        limits: NotificationLimits,
        query_limits: NotificationQueryLimits,
    ) -> None:
        self.service, self.sessions, self.limits, self.query_limits = (
            service,
            service.sessions,
            limits,
            query_limits,
        )

    def _authority(
        self, session: Session, subject: ExternalSubject, expected_actor: UUID | None = None
    ) -> UUID:
        if (subject.provider, subject.namespace) != (
            self.service.namespace.provider,
            self.service.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "Notification workspace differs", 403)
        principal = self.service.access.resolve_external_in_transaction(session, subject)
        if principal is None or (
            expected_actor is not None and principal.user_id != expected_actor
        ):
            raise DomainError(
                "PERMISSION_DENIED", "Current notification administrator is unavailable", 403
            )
        self.service.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=principal.user_id,
                permission=MANAGE_NOTIFICATIONS,
                scope=AccessScope(type="global", id="*"),
            ),
        )
        return principal.user_id

    def _budget(self, session: Session) -> None:
        transaction_budget(
            session,
            self.query_limits.lock_timeout_ms,
            self.query_limits.statement_timeout_ms,
            self.query_limits.transaction_timeout_ms,
        )

    def _view(
        self,
        session: Session,
        record: NotificationOutbox,
        attempts: tuple[NotificationAttempt, ...],
    ) -> NotificationOperatorView:
        try:
            content: NoticeContent | None = NoticeContent.model_validate(record.content)
            if self.service.record_fingerprint(record) != record.fingerprint:
                content = None
        except ValidationError:
            content = None
        known_success = sum(item.state in {"delivered", "late_delivered"} for item in attempts)
        code = None
        if record.state != "uncertain":
            code = "STATE_CHANGED"
        elif not attempts or attempts[-1].state != "uncertain":
            code = "ATTEMPT_UNAVAILABLE"
        elif known_success:
            code = "ALREADY_DELIVERED"
        elif record.attempts >= self.limits.max_attempts:
            code = "RETRY_BUDGET_EXHAUSTED"
        else:
            try:
                self.service.validate_snapshot(session, record)
            except DomainError as rejected:
                code = rejected.code
        return NotificationOperatorView(
            notification_id=record.id,
            recipient_user_id=record.recipient_user_id,
            target=ExternalSubject(
                provider=record.provider, namespace=record.namespace, external_id=record.external_id
            ),
            template_id=record.template_id,
            created_at=utc(record.created_at),
            state=record.state,
            total_attempts=record.attempts,
            maximum_attempts=self.limits.max_attempts,
            last_attempt_id=record.last_attempt_id,
            attempts=tuple(
                NotificationAttemptView(
                    attempt_id=item.id,
                    number=item.attempt,
                    state=item.state,
                    started_at=utc(item.started_at),
                    prepared_at=utc(item.prepared_at) if item.prepared_at else None,
                    finished_at=utc(item.finished_at) if item.finished_at else None,
                    error_code=item.error_code,
                    channel_ref=item.channel_ref,
                    message_ref=item.message_ref,
                )
                for item in attempts
            ),
            known_successful_attempts=known_success,
            content=content,
            retry=NotificationRetryEligibility(allowed=code is None, code=code),
        )

    def _views(
        self, session: Session, records: tuple[NotificationOutbox, ...]
    ) -> tuple[NotificationOperatorView, ...]:
        attempts = NotificationRepository(session).attempt_evidence(
            tuple(item.id for item in records)
        )
        if len(attempts) > 400:
            raise DomainError(
                "NOTIFICATION_QUERY_UNAVAILABLE",
                "Notification evidence exceeds the page budget",
                503,
            )
        return tuple(
            self._view(
                session,
                record,
                tuple(attempt for attempt in attempts if attempt.notification_id == record.id),
            )
            for record in records
        )

    def page(
        self, subject: ExternalSubject, query: UncertainNotificationsQuery
    ) -> UncertainNotificationsPage:
        try:
            with self.sessions() as session, session.no_autoflush:
                self._budget(session)
                actor_id = self._authority(session, subject)
                fetched = NotificationRepository(session).uncertain_page(
                    query,
                    namespace=self.service.namespace,
                    template_ids=tuple(self.service.templates),
                )
                records = fetched[: query.limit]
                result = UncertainNotificationsPage(
                    namespace=self.service.namespace,
                    generated_at=self.service.now(),
                    items=self._views(session, records),
                    next_cursor=NotificationCursor(
                        created_at=utc(records[-1].created_at), notification_id=records[-1].id
                    )
                    if len(fetched) > query.limit
                    else None,
                )
                self._authority(session, subject, actor_id)
                return result
        except DBAPIError:
            raise DomainError(
                "NOTIFICATION_QUERY_UNAVAILABLE", "Notification query is unavailable", 503
            ) from None
        except (ValidationError, ValueError, TypeError):
            raise DomainError(
                "NOTIFICATION_QUERY_UNAVAILABLE", "Notification evidence is invalid", 503
            ) from None

    def detail(self, subject: ExternalSubject, notification_id: UUID) -> NotificationOperatorView:
        try:
            with self.sessions() as session, session.no_autoflush:
                self._budget(session)
                actor_id = self._authority(session, subject)
                record = NotificationRepository(session).scoped_notification(
                    str(notification_id),
                    namespace=self.service.namespace,
                    template_ids=tuple(self.service.templates),
                )
                if record is None:
                    raise DomainError("NOT_FOUND", "Notification is unavailable", 404)
                result = self._views(session, (record,))[0]
                self._authority(session, subject, actor_id)
                return result
        except DBAPIError:
            raise DomainError(
                "NOTIFICATION_QUERY_UNAVAILABLE", "Notification query is unavailable", 503
            ) from None
        except (ValidationError, ValueError, TypeError):
            raise DomainError(
                "NOTIFICATION_QUERY_UNAVAILABLE", "Notification evidence is invalid", 503
            ) from None
