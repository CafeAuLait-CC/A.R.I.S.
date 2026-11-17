from datetime import timedelta

import pytest
from sqlalchemy import select

from aris.core.audit import AuditEntry
from aris.core.notifications.dispatch import NotificationDispatcher
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.schemas import NotificationTemplateDefinition
from aris.core.notifications.service import NotificationService
from tests.test_command_intake import NAMESPACE
from tests.test_event_relay import seed
from tests.test_notifications import (
    LIMITS,
    RECEIPT,
    FixtureTemplate,
    FixtureTransport,
    enqueue,
    intent,
)
from tests.test_notifications import command_state as command_state
from tests.test_notifications import durable_state as durable_state
from tests.test_notifications import notification_state as notification_state
from tests.test_notifications import relay_state as relay_state


def snapshot(sessions):
    with sessions() as session:
        return tuple(
            tuple(
                tuple(row)
                for row in session.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key.columns)
                )
            )
            for model in (NotificationOutbox, NotificationAttempt, AuditEntry)
        )


def foreign_dispatcher(state, change):
    _, _, sessions, _, _, _, clock, _ = state
    namespace = NAMESPACE.model_copy(
        update={
            **({"namespace": "TOTHER"} if change == "workspace" else {}),
            **({"provider": "other"} if change == "provider" else {}),
        }
    )
    service = NotificationService(
        sessions,
        namespace,
        [] if change == "template" else [FixtureTemplate()],
        clock=lambda: clock["now"],
    )
    return NotificationDispatcher(service, LIMITS, FixtureTransport(sessions))


@pytest.mark.parametrize("change", ["workspace", "provider", "template"])
@pytest.mark.parametrize("phase", ["queued", "claimed", "sending", "expired_sending"])
def test_wrong_notification_worker_cannot_claim_prepare_or_complete_foreign_work(
    notification_state, change, phase
):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    handle = enqueue(notification_state)
    claim = None
    if phase != "queued":
        claim = dispatcher.claim()
    if phase in {"sending", "expired_sending"}:
        dispatcher.prepare(claim)
    if phase == "expired_sending":
        clock["now"] = claim.lease_expires_at + timedelta(seconds=1)
    wrong = foreign_dispatcher(notification_state, change)
    before = snapshot(sessions)
    assert wrong.claim() is None
    if claim is not None:
        assert wrong.prepare(claim).state == "lost_claim"
        assert wrong.finish(claim, receipt=RECEIPT).state == "lost_claim"
    assert snapshot(sessions) == before and not wrong.transport.sent
    if phase == "queued":
        assert dispatcher.run_once().state == "delivered"
    elif phase == "claimed":
        dispatcher.prepare(claim)
        assert dispatcher.finish(claim, receipt=RECEIPT).state == "delivered"
    elif phase == "sending":
        assert dispatcher.finish(claim, receipt=RECEIPT).state == "delivered"
    else:
        assert dispatcher.run_once().state == "uncertain"
    with sessions() as session:
        assert session.get(NotificationOutbox, str(handle.notification_id)).attempts == 1
    assert len(transport.sent) == (1 if phase == "queued" else 0)


def test_unregistered_older_template_does_not_block_eligible_notification(notification_state):
    _, _, sessions, _, _, _, clock, relay_state = notification_state
    older = enqueue(notification_state)

    class OtherTemplate(FixtureTemplate):
        definition = NotificationTemplateDefinition(template_id="fixture.other", revision="1")

    service = NotificationService(
        sessions, NAMESPACE, [OtherTemplate()], clock=lambda: clock["now"]
    )
    event_id = seed(relay_state)
    with sessions.begin() as session:
        newer = service.enqueue(
            session,
            intent(notification_state, event_id).model_copy(
                update={"template_id": "fixture.other"}
            ),
        )
    transport = FixtureTransport(sessions)
    dispatcher = NotificationDispatcher(service, LIMITS, transport)
    assert dispatcher.run_once().notification_id == newer.notification_id
    assert dispatcher.run_once() is None
    with sessions() as session:
        original = session.get(NotificationOutbox, str(older.notification_id))
        assert original.state == "queued" and original.attempts == 0
    assert len(transport.sent) == 1
