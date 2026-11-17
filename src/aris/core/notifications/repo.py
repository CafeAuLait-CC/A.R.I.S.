from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.query_schemas import UncertainNotificationsQuery


class NotificationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_or_existing(self, values: dict[str, Any]) -> tuple[NotificationOutbox, bool]:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        inserted = self.session.scalar(
            insert(NotificationOutbox)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["dedupe_sha256"])
            .returning(NotificationOutbox.id)
        )
        record = self.session.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.dedupe_sha256 == values["dedupe_sha256"])
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        return record, inserted is not None

    def notification(
        self, notification_id: str, *, lock: bool = False
    ) -> NotificationOutbox | None:
        statement = select(NotificationOutbox).where(NotificationOutbox.id == notification_id)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement.execution_options(populate_existing=True))

    def attempt(self, attempt_id: str) -> NotificationAttempt | None:
        return self.session.get(NotificationAttempt, attempt_id, populate_existing=True)

    def has_successful_attempt(self, notification_id: str) -> bool:
        return (
            self.session.scalar(
                select(NotificationAttempt.id)
                .where(
                    NotificationAttempt.notification_id == notification_id,
                    NotificationAttempt.state.in_(("delivered", "late_delivered")),
                )
                .limit(1)
            )
            is not None
        )

    def uncertain_page(
        self,
        query: UncertainNotificationsQuery,
        *,
        namespace: IdentityNamespace,
        template_ids: tuple[str, ...],
    ) -> tuple[NotificationOutbox, ...]:
        statement = select(NotificationOutbox).where(
            NotificationOutbox.provider == namespace.provider,
            NotificationOutbox.namespace == namespace.namespace,
            NotificationOutbox.template_id.in_(template_ids),
            NotificationOutbox.state == "uncertain",
        )
        if query.after is not None:
            statement = statement.where(
                or_(
                    NotificationOutbox.created_at > query.after.created_at,
                    and_(
                        NotificationOutbox.created_at == query.after.created_at,
                        NotificationOutbox.id > str(query.after.notification_id),
                    ),
                )
            )
        return tuple(
            self.session.scalars(
                statement.order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                .limit(query.limit + 1)
                .execution_options(populate_existing=True)
            )
        )

    def attempt_evidence(
        self, notification_ids: tuple[str, ...]
    ) -> tuple[NotificationAttempt, ...]:
        return tuple(
            self.session.scalars(
                select(NotificationAttempt)
                .where(NotificationAttempt.notification_id.in_(notification_ids))
                .order_by(NotificationAttempt.notification_id, NotificationAttempt.attempt)
                .limit(401)
                .execution_options(populate_existing=True)
            )
        )

    def scoped_notification(
        self,
        notification_id: str,
        *,
        namespace: IdentityNamespace,
        template_ids: tuple[str, ...],
        lock: bool = False,
    ) -> NotificationOutbox | None:
        statement = select(NotificationOutbox).where(
            NotificationOutbox.id == notification_id,
            NotificationOutbox.provider == namespace.provider,
            NotificationOutbox.namespace == namespace.namespace,
            NotificationOutbox.template_id.in_(template_ids),
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement.execution_options(populate_existing=True))

    def next_notification(
        self, now: datetime, *, namespace: IdentityNamespace, template_ids: tuple[str, ...]
    ) -> NotificationOutbox | None:
        return self.session.scalar(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.provider == namespace.provider,
                NotificationOutbox.namespace == namespace.namespace,
                NotificationOutbox.template_id.in_(template_ids),
                or_(
                    and_(
                        NotificationOutbox.state == "queued", NotificationOutbox.available_at <= now
                    ),
                    and_(
                        NotificationOutbox.state == "running",
                        NotificationOutbox.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(
                NotificationOutbox.available_at,
                NotificationOutbox.created_at,
                NotificationOutbox.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
