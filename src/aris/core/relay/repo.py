from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from aris.core.events import DomainEvent, OutboxEntry
from aris.core.relay.models import ConsumerInbox, EventDelivery, EventPublication
from aris.core.relay.schemas import EventSelector


class EventRelayRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def unpublished(
        self, selectors: Sequence[EventSelector], limit: int
    ) -> Sequence[tuple[OutboxEntry, DomainEvent]]:
        if not selectors:
            return ()
        statement = (
            select(OutboxEntry, DomainEvent)
            .join(DomainEvent, DomainEvent.event_id == OutboxEntry.event_id)
            .where(
                OutboxEntry.published_at.is_(None),
                or_(
                    *(
                        and_(
                            DomainEvent.event_type == item.event_type,
                            DomainEvent.event_version == item.event_version,
                        )
                        for item in selectors
                    )
                ),
            )
            .order_by(OutboxEntry.created_at, OutboxEntry.event_id)
            .limit(limit)
            .with_for_update(of=OutboxEntry, skip_locked=True)
        )
        return [(row[0], row[1]) for row in self.session.execute(statement)]

    def event(self, event_id: str) -> DomainEvent | None:
        return self.session.get(DomainEvent, event_id, populate_existing=True)

    def publication(self, event_id: str) -> EventPublication | None:
        return self.session.get(EventPublication, event_id, populate_existing=True)

    def delivery(self, delivery_id: str, *, lock: bool = False) -> EventDelivery | None:
        statement = select(EventDelivery).where(EventDelivery.id == delivery_id)
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement.execution_options(populate_existing=True))

    def next_delivery(self, now: datetime, consumer_ids: Sequence[str]) -> EventDelivery | None:
        if not consumer_ids:
            return None
        return self.session.scalar(
            select(EventDelivery)
            .where(
                EventDelivery.consumer_id.in_(consumer_ids),
                or_(
                    and_(EventDelivery.state == "queued", EventDelivery.available_at <= now),
                    and_(EventDelivery.state == "running", EventDelivery.lease_expires_at <= now),
                ),
            )
            .order_by(EventDelivery.available_at, EventDelivery.created_at, EventDelivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    def inbox(self, delivery: EventDelivery) -> ConsumerInbox | None:
        return self.session.get(ConsumerInbox, (delivery.consumer_id, delivery.event_id))
