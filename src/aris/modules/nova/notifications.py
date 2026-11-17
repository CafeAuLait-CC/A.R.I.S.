from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.notifications.schemas import (
    NoticeContent,
    NoticeField,
    NotificationIntent,
    NotificationTemplateDefinition,
)
from aris.core.notifications.service import NotificationService, NotificationTemplate
from aris.core.relay.schemas import ConsumerDefinition, EventEnvelope, EventSelector
from aris.core.relay.service import DatabaseEventConsumer
from aris.core.time import utc
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.reservation_schemas import ReservationView
from aris.modules.vega.target_schemas import GpuTargetQuery
from aris.modules.vega.target_service import VegaTargetService

ReservationNoticeType = Literal[
    "reservation.created", "reservation.cancelled", "reservation.released", "reservation.extended"
]
TYPES = {
    "reservation.created": "confirmed",
    "reservation.cancelled": "cancelled",
    "reservation.released": "completed",
    "reservation.extended": "confirmed",
}
DATETIME = TypeAdapter(AwareDatetime)


class ReservationNoticeFacts(AccessContract):
    event_id: UUID
    reservation_id: UUID
    owner_id: UUID
    gpu_uuid: str
    event_type: ReservationNoticeType
    version: int = Field(ge=1)
    start_at: AwareDatetime
    end_at: AwareDatetime
    occurred_at: AwareDatetime
    administrative: bool = Field(strict=True)
    effective_end_at: AwareDatetime | None = None


class ReservationNoticeFactsService:
    def facts(self, session: Session, envelope: EventEnvelope) -> ReservationNoticeFacts:
        with session.no_autoflush:
            stored = ReservationRepository(session).notice_record(
                str(envelope.aggregate_id), envelope.aggregate_version
            )
        if stored is None:
            raise DomainError(
                "NOTICE_EVIDENCE_MISSING", "Reservation notification history is missing", 409
            )
        try:
            payload = envelope.payload
            view = ReservationView.model_validate(stored.payload["reservation"])
            origin = ReservationView.model_validate(stored.origin_payload["reservation"])
            owner = UUID(stored.owner_id)
            administrative = payload["administrative"]
            if not isinstance(administrative, bool):
                raise ValueError("invalid administrative marker")
            if (
                envelope.event_version != 1
                or envelope.aggregate_type != "reservation"
                or envelope.event_type not in TYPES
                or stored.event_type != envelope.event_type
                or stored.actor != envelope.actor
                or utc(stored.occurred_at) != envelope.occurred_at
                or UUID(stored.origin_actor) != owner
                or UUID(str(payload["owner_id"])) != owner
                or UUID(str(payload["reservation_id"])) != view.id
                or str(view.id) != envelope.aggregate_id
                or origin.id != view.id
                or payload["gpu_uuid"] != stored.gpu_uuid
                or view.gpu_uuid != stored.gpu_uuid
                or origin.gpu_uuid != stored.gpu_uuid
                or origin.version != 1
                or origin.status != "confirmed"
                or origin.updated_at != utc(stored.origin_at)
                or view.version != envelope.aggregate_version
                or view.start_at != origin.start_at
                or view.end_at <= view.start_at
                or view.updated_at != envelope.occurred_at
                or UUID(str(stored.payload["command_id"])) != envelope.causation_id
                or stored.payload["request_id"] != envelope.correlation_id
                or (not administrative and UUID(envelope.actor) != owner)
                or (
                    administrative
                    and (
                        stored.payload.get("administrative") is not True
                        or UUID(str(stored.payload["owner_id"])) != owner
                    )
                )
            ):
                raise ValueError("reservation event differs from historical record")
            allowed_statuses = (
                {"confirmed", "active"}
                if envelope.event_type == "reservation.extended"
                else {TYPES[envelope.event_type]}
            )
            if view.status not in allowed_statuses:
                raise ValueError("reservation status differs")
            for field, expected in (
                ("version", envelope.aggregate_version),
                ("status", view.status),
            ):
                if field in payload and payload[field] != expected:
                    raise ValueError("reservation body differs")
            effective = stored.payload.get("effective_end_at")
            facts = ReservationNoticeFacts(
                event_id=envelope.event_id,
                reservation_id=view.id,
                owner_id=owner,
                gpu_uuid=view.gpu_uuid,
                event_type=envelope.event_type,
                version=envelope.aggregate_version,
                start_at=view.start_at,
                end_at=view.end_at,
                occurred_at=envelope.occurred_at,
                administrative=administrative,
                effective_end_at=effective,
            )
            required_times = (
                {"start_at", "end_at"}
                if envelope.event_type == "reservation.created"
                else {"end_at"}
                if envelope.event_type == "reservation.extended"
                else set()
            )
            for field in ("start_at", "end_at"):
                if field in payload or field in required_times:
                    value = payload[field]
                    if not isinstance(value, str) or DATETIME.validate_python(value) != getattr(
                        facts, field
                    ):
                        raise ValueError("reservation event window differs")
            if envelope.event_type == "reservation.released":
                if (
                    facts.effective_end_at != min(facts.occurred_at, facts.end_at)
                    or facts.occurred_at < facts.start_at
                    or stored.payload.get("released_at") != payload.get("released_at")
                    or stored.payload.get("effective_end_at") != payload.get("effective_end_at")
                ):
                    raise ValueError("release evidence differs")
            elif effective is not None:
                raise ValueError("unexpected release evidence")
            return facts
        except (ValidationError, KeyError, ValueError, TypeError):
            raise DomainError(
                "NOTICE_EVIDENCE_INVALID", "Reservation notification evidence differs", 409
            ) from None


class ReservationNoticeInput(AccessContract):
    event_type: ReservationNoticeType
    resource: str = Field(min_length=1, max_length=512)
    version: int = Field(ge=1)
    start_at: AwareDatetime
    end_at: AwareDatetime
    occurred_at: AwareDatetime
    administrative: bool = Field(strict=True)
    effective_end_at: AwareDatetime | None


def timestamp(value: AwareDatetime) -> str:
    return utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")


class ReservationNoticeTemplate(NotificationTemplate):
    definition = NotificationTemplateDefinition(template_id="nova.reservation.update", revision="1")

    def render(self, parameters: dict[str, JsonValue]) -> NoticeContent:
        facts = ReservationNoticeInput.model_validate(parameters)
        verb = {
            "reservation.created": "confirmed",
            "reservation.cancelled": "cancelled",
            "reservation.released": "released",
            "reservation.extended": "extended",
        }[facts.event_type]
        summary = (
            f"An administrator {verb} your reservation."
            if facts.administrative
            else f"Your reservation was {verb}."
        )
        fields = [
            NoticeField(label="Resource (last known)", value=facts.resource),
            NoticeField(label="Scheduled start", value=timestamp(facts.start_at)),
            NoticeField(label="Scheduled end", value=timestamp(facts.end_at)),
            NoticeField(
                label="Recorded update", value=f"{facts.version} · {timestamp(facts.occurred_at)}"
            ),
        ]
        if facts.effective_end_at is not None:
            fields.append(
                NoticeField(label="Reservation ended", value=timestamp(facts.effective_end_at))
            )
        return NoticeContent(title="Reservation " + verb, summary=summary, fields=tuple(fields))


class ReservationNotificationConsumer(DatabaseEventConsumer):
    definition = ConsumerDefinition(
        consumer_id="nova.reservation.notifications",
        revision="1",
        events=tuple(
            EventSelector(event_type=event_type, event_version=1) for event_type in sorted(TYPES)
        ),
    )

    def __init__(self, notifications: NotificationService, labels: VegaTargetService) -> None:
        self.notifications, self.labels = notifications, labels
        self.facts = ReservationNoticeFactsService()

    def consume(self, session: Session, envelope: EventEnvelope) -> None:
        facts = self.facts.facts(session, envelope)
        label = self.labels.label_in_transaction(session, GpuTargetQuery(gpu_uuid=facts.gpu_uuid))
        resource = (
            f"{label.node_id} · GPU {label.index} · {label.model}" if label else "Unavailable"
        )
        body = ReservationNoticeInput(
            event_type=facts.event_type,
            resource=resource,
            version=facts.version,
            start_at=facts.start_at,
            end_at=facts.end_at,
            occurred_at=facts.occurred_at,
            administrative=facts.administrative,
            effective_end_at=facts.effective_end_at,
        )
        self.notifications.enqueue(
            session,
            NotificationIntent(
                event_id=facts.event_id,
                recipient_user_id=facts.owner_id,
                template_id=ReservationNoticeTemplate.definition.template_id,
                parameters=body.model_dump(mode="json"),
                request_id=envelope.correlation_id,
            ),
        )
