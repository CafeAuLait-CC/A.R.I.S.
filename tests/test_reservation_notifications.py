import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text

from aris.core.access.models import ExternalIdentity, User
from aris.core.events import DomainEvent
from aris.core.notifications.dispatch import NotificationDispatcher
from aris.core.notifications.models import NotificationOutbox
from aris.core.notifications.service import NotificationService
from aris.core.relay.models import ConsumerInbox
from aris.core.relay.schemas import EventEnvelope
from aris.core.relay.service import EventRelay, event_data
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.notifications import (
    ReservationNoticeFactsService,
    ReservationNoticeTemplate,
    ReservationNotificationConsumer,
)
from aris.modules.vega.models import GpuDevice
from aris.modules.vega.target_schemas import GpuTargetQuery
from aris.modules.vega.target_service import VegaTargetService
from tests.test_command_intake import NAMESPACE
from tests.test_event_relay import LIMITS as RELAY_LIMITS
from tests.test_notifications import LIMITS as NOTIFICATION_LIMITS
from tests.test_notifications import FixtureTransport
from tests.test_reservation_administration import administration as administration
from tests.test_reservation_administration import pending as admin_pending
from tests.test_reservation_cancellation import cancelled
from tests.test_reservation_cancellation import cancelling as cancelling
from tests.test_reservation_extension import extending as extending
from tests.test_reservation_extension import request as extension_request
from tests.test_reservation_release import release
from tests.test_reservation_release import releasing as releasing
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import confirm, context, create


def setup(state, settings):
    _, _, sessions, ids, clock, _ = state
    with sessions.begin() as session:
        for user in ("alice", "bob"):
            session.add(
                ExternalIdentity(
                    id=str(uuid4()),
                    user_id=ids[user],
                    provider="slack",
                    namespace=NAMESPACE.namespace,
                    external_id="U_" + user,
                )
            )
    labels = VegaTargetService(sessions, settings, clock=lambda: clock["now"])
    notifications = NotificationService(
        sessions, NAMESPACE, [ReservationNoticeTemplate()], clock=lambda: clock["now"]
    )
    consumer = ReservationNotificationConsumer(notifications, labels)
    relay = EventRelay(sessions, [consumer], RELAY_LIMITS, clock=lambda: clock["now"])
    transport = FixtureTransport(sessions)
    dispatch = NotificationDispatcher(notifications, NOTIFICATION_LIMITS, transport)
    return consumer, relay, dispatch, transport


def process(relay, expected):
    assert relay.publish_once().published == expected
    for _ in range(expected):
        result = relay.run_once()
        assert result.state == "processed", result
    assert relay.run_once() is None


def contents(sessions):
    with sessions() as session:
        return {
            event_type: (record, record.content)
            for record, event_type in session.execute(
                select(NotificationOutbox, DomainEvent.event_type).join(
                    DomainEvent, DomainEvent.event_id == NotificationOutbox.event_id
                )
            )
        }


def test_created_reservation_notifies_owner_using_real_inventory_without_private_purpose(
    booking, settings
):
    _, _, sessions, ids, _, _ = booking
    _, relay, dispatch, transport = setup(booking, settings)
    create(booking)
    process(relay, 1)
    record, body = contents(sessions)["reservation.created"]
    assert record.recipient_user_id == ids["alice"] and body["title"] == "Reservation confirmed"
    assert "node-a · GPU 0 · Fixture" in json.dumps(body, ensure_ascii=False)
    assert "Private fixture purpose" not in json.dumps(body)
    assert "GPU-target" not in json.dumps(body) and not transport.sent
    assert dispatch.run_once().state == "delivered" and len(transport.sent) == 1
    assert transport.sent[0].target.external_id == "U_alice"


def test_old_created_event_reads_its_original_window_after_later_extension(extending, settings):
    _, commands, sessions, _, _, _ = extending
    _, relay, _, _ = setup(extending, settings)
    created = create(extending).result["reservation"]
    waiting = commands.execute(context(extending, key="extend"), extension_request(created["id"]))
    confirm(extending, waiting)
    process(relay, 2)
    bodies = contents(sessions)
    initial = {item["label"]: item["value"] for item in bodies["reservation.created"][1]["fields"]}
    later = {item["label"]: item["value"] for item in bodies["reservation.extended"][1]["fields"]}
    assert initial["Scheduled end"] != later["Scheduled end"]
    assert initial["Recorded update"].startswith("1 ·") and later["Recorded update"].startswith(
        "2 ·"
    )
    with sessions() as session:
        source = session.scalar(
            select(DomainEvent).where(DomainEvent.event_type == "reservation.created")
        )
        facts = ReservationNoticeFactsService().facts(
            session, EventEnvelope.model_validate(event_data(source))
        )
        assert facts.end_at.isoformat().replace("+00:00", "Z") == created["end_at"]


def test_owner_cancellation_uses_recorded_snapshot_and_no_reason_or_purpose(cancelling, settings):
    _, _, sessions, _, _, _ = cancelling
    _, relay, _, _ = setup(cancelling, settings)
    created = create(cancelling).result["reservation"]
    cancelled(cancelling, created["id"])
    process(relay, 2)
    body = contents(sessions)["reservation.cancelled"][1]
    assert body["summary"] == "Your reservation was cancelled."
    assert "Owner changed their plan" not in json.dumps(
        body
    ) and "Private fixture purpose" not in json.dumps(body)


@pytest.mark.parametrize("late", [False, True])
def test_release_keeps_scheduled_window_separate_from_effective_end(releasing, settings, late):
    _, _, sessions, _, clock, _ = releasing
    _, relay, _, _ = setup(releasing, settings)
    created = create(releasing).result["reservation"]
    clock["now"] += timedelta(hours=3 if late else 1, minutes=0 if late else 20)
    release(releasing, created["id"])
    process(relay, 2)
    fields = {
        item["label"]: item["value"]
        for item in contents(sessions)["reservation.released"][1]["fields"]
    }
    assert (fields["Reservation ended"] == fields["Scheduled end"]) is late
    assert fields["Recorded update"].startswith("2 ·")


@pytest.mark.parametrize("missing_label", [False, True])
def test_administrator_notices_go_to_owner_and_missing_inventory_is_not_fabricated(
    administration, settings, missing_label
):
    _, _, sessions, ids, _, _ = administration
    _, relay, dispatch, transport = setup(administration, settings)
    created = create(administration).result["reservation"]
    if missing_label:
        with sessions.begin() as session:
            session.execute(delete(GpuDevice))
    confirm(administration, admin_pending(administration, created["id"]), user="bob")
    process(relay, 2)
    record, body = contents(sessions)["reservation.cancelled"]
    assert record.recipient_user_id == ids["alice"] and record.recipient_user_id != ids["bob"]
    assert body["summary"] == "An administrator cancelled your reservation."
    if missing_label:
        assert body["fields"][0]["value"] == "Unavailable"
    assert ids["bob"] not in json.dumps(body) and "Private fixture purpose" not in json.dumps(body)
    while dispatch.run_once() is not None:
        pass
    assert len(transport.sent) == 2
    assert all(item.target.external_id == "U_alice" for item in transport.sent)


def test_inactive_owner_is_not_replaced_by_administrator_and_committed_cancel_remains(
    administration, settings
):
    _, _, sessions, ids, _, _ = administration
    _, relay, dispatch, transport = setup(administration, settings)
    created = create(administration).result["reservation"]
    with sessions.begin() as session:
        session.get(User, ids["alice"]).status = "suspended"
    confirm(administration, admin_pending(administration, created["id"]), user="bob")
    assert relay.publish_once().published == 2
    assert relay.run_once().state == relay.run_once().state == "dead"
    assert dispatch.run_once() is None and not transport.sent
    with sessions() as session:
        assert session.scalar(select(Reservation.status)) == "cancelled"
        assert session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
        assert session.scalar(select(func.count()).select_from(ConsumerInbox)) == 0


@pytest.mark.parametrize(
    "bad", ["owner", "gpu", "actor", "cause", "window", "history", "reassigned_owner"]
)
def test_inconsistent_event_or_history_cannot_generate_notice_to_any_recipient(
    booking, settings, bad
):
    _, _, sessions, ids, _, _ = booking
    _, relay, dispatch, transport = setup(booking, settings)
    create(booking)
    with sessions.begin() as session:
        event = session.scalar(select(DomainEvent))
        data = dict(event.payload)
        if bad == "owner":
            data["owner_id"] = ids["bob"]
        elif bad == "gpu":
            data["gpu_uuid"] = "other-gpu"
        elif bad == "actor":
            event.actor = ids["bob"]
        elif bad == "cause":
            event.causation_id = str(uuid4())
        elif bad == "window":
            data["end_at"] = data["start_at"]
        elif bad == "reassigned_owner":
            session.scalar(select(Reservation)).owner_id = ids["bob"]
        else:
            history = session.scalar(select(ReservationEvent))
            history.payload = {**history.payload, "command_id": str(uuid4())}
        event.payload = data
    assert relay.publish_once().published == 1
    result = relay.run_once()
    assert result.state == "dead" and result.code == "NOTICE_EVIDENCE_INVALID"
    assert dispatch.run_once() is None and not transport.sent
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0


def test_postgres_notice_facts_and_inventory_labels_are_read_only_and_do_not_flush_owner_work(
    booking, settings
):
    _, _, sessions, _, _, _ = booking
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY transaction")
    create(booking)
    labels = VegaTargetService(sessions, settings)
    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        envelope = EventEnvelope.model_validate(event_data(session.scalar(select(DomainEvent))))
        pending_user = User(
            id=str(uuid4()), display_name="Pending", status="active", timezone="UTC"
        )
        session.add(pending_user)
        facts = ReservationNoticeFactsService().facts(session, envelope)
        assert (
            labels.label_in_transaction(session, GpuTargetQuery(gpu_uuid=facts.gpu_uuid)).index == 0
        )
        assert pending_user in session.new and session.in_transaction()
        session.rollback()
