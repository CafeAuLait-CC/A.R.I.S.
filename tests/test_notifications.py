import json
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aris.core.access.models import ExternalIdentity, User
from aris.core.access.schemas import AccessContract
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.notifications.dispatch import NotificationDispatcher
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.schemas import (
    NoticeContent,
    NotificationDeliveryError,
    NotificationIntent,
    NotificationLimits,
    NotificationTemplateDefinition,
    TransportReceipt,
)
from aris.core.notifications.service import NotificationService, NotificationTemplate
from aris.core.relay.models import ConsumerInbox
from aris.core.relay.service import EventRelay
from aris.core.time import utc
from tests.test_command_intake import NAMESPACE
from tests.test_event_relay import LIMITS as RELAY_LIMITS
from tests.test_event_relay import ProfileConsumer, seed
from tests.test_event_relay import command_state as command_state
from tests.test_event_relay import durable_state as durable_state
from tests.test_event_relay import relay_state as relay_state

LIMITS = NotificationLimits(
    lock_timeout_ms=200,
    statement_timeout_ms=500,
    transaction_timeout_ms=1000,
    lease_seconds=5,
    max_attempts=3,
    retry_base_seconds=1,
)
RECEIPT = TransportReceipt(channel_ref="DFIXTURE", message_ref="1731800000.000001")


class NoticeInput(AccessContract):
    text: str = Field(min_length=1, max_length=1000)


class FixtureTemplate(NotificationTemplate):
    definition = NotificationTemplateDefinition(template_id="fixture.notice", revision="1")

    def render(self, parameters):
        return NoticeContent(
            title="Fixture notification", summary=NoticeInput.model_validate(parameters).text
        )


class FixtureTransport:
    def __init__(self, sessions):
        self.sessions, self.sent, self.error = sessions, [], None

    def send(self, prepared):
        # A distinct transaction must see preparation committed before any I/O.
        with self.sessions() as session:
            assert session.get(NotificationOutbox, str(prepared.notification_id)).state == "running"
            assert session.get(NotificationAttempt, str(prepared.attempt_id)).state == "sending"
        self.sent.append(prepared)
        if self.error is not None:
            raise self.error
        return RECEIPT


@pytest.fixture
def notification_state(relay_state):
    _, sessions, ids, _, clock, _ = relay_state
    template = FixtureTemplate()
    service = NotificationService(sessions, NAMESPACE, [template], clock=lambda: clock["now"])
    transport = FixtureTransport(sessions)
    dispatcher = NotificationDispatcher(service, LIMITS, transport)
    return service, dispatcher, sessions, ids, template, transport, clock, relay_state


def intent(state, event_id, text="Fixture event recorded"):
    return NotificationIntent(
        event_id=event_id,
        recipient_user_id=state[3]["alice"],
        template_id="fixture.notice",
        parameters={"text": text},
        request_id="fixture-notification",
    )


def enqueue(state, *, event_id=None, text="Fixture event recorded"):
    service, _, sessions, _, _, _, _, relay_state = state
    event_id = event_id or seed(relay_state)
    with sessions.begin() as session:
        return service.enqueue(session, intent(state, event_id, text))


def counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (NotificationOutbox, NotificationAttempt)
        )


def test_notification_enqueues_only_then_delivers_after_commit_with_receipt(notification_state):
    _, dispatcher, sessions, _, _, transport, _, _ = notification_state
    handle = enqueue(notification_state)
    assert handle.state == "queued" and not handle.duplicate and not transport.sent
    assert counts(sessions) == (1, 0)
    result = dispatcher.run_once()
    assert result.state == "delivered" and len(transport.sent) == 1
    assert dispatcher.run_once() is None
    with sessions() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        attempt = session.scalar(select(NotificationAttempt))
        assert record.state == attempt.state == "delivered"
        assert (
            attempt.channel_ref == RECEIPT.channel_ref
            and attempt.message_ref == RECEIPT.message_ref
        )
        assert record.lease_token is None and record.completed_at is not None
        audit = list(session.scalars(select(AuditEntry)))
        assert "notification.delivered" in [row.action for row in audit]
        assert "Fixture event recorded" not in json.dumps([row.changes for row in audit])


def test_enqueue_obeys_owner_rollback_and_cannot_send_uncommitted_notice(notification_state):
    service, dispatcher, sessions, _, _, transport, _, relay_state = notification_state
    event_id = seed(relay_state)
    with pytest.raises(RuntimeError):
        with sessions.begin() as session:
            service.enqueue(session, intent(notification_state, event_id))
            assert counts(sessions) == (0, 0)
            assert dispatcher.run_once() is None
            raise RuntimeError("fixture-owner-rollback")
    assert counts(sessions) == (0, 0) and not transport.sent


def test_stable_source_user_template_deduplicates_and_content_conflict_is_atomic(
    notification_state,
):
    _, _, sessions, _, _, transport, _, relay_state = notification_state
    event_id = seed(relay_state)
    first = enqueue(notification_state, event_id=event_id)
    again = enqueue(notification_state, event_id=event_id)
    assert again.notification_id == first.notification_id and again.duplicate
    with pytest.raises(DomainError) as conflict:
        enqueue(notification_state, event_id=event_id, text="Changed intent")
    assert conflict.value.code == "CONFLICT" and counts(sessions) == (1, 0)
    assert not transport.sent
    with sessions() as session:
        assert list(session.scalars(select(AuditEntry.action))).count("notification.queued") == 1


@pytest.mark.parametrize("wrong", ["unmapped", "suspended", "workspace", "ambiguous"])
def test_recipient_must_be_current_active_and_unambiguous_in_fixed_namespace(
    notification_state, wrong
):
    _, _, sessions, ids, _, transport, _, _ = notification_state
    with sessions.begin() as session:
        identity = session.scalar(
            select(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"])
        )
        if wrong == "unmapped":
            session.delete(identity)
        elif wrong == "suspended":
            session.get(User, ids["alice"]).status = "suspended"
        elif wrong == "workspace":
            identity.namespace = "T_DIFFERENT"
        else:
            session.add(
                ExternalIdentity(
                    id=str(uuid4()),
                    user_id=ids["alice"],
                    provider="slack",
                    namespace=NAMESPACE.namespace,
                    external_id="U_SECOND_IDENTITY",
                )
            )
    with pytest.raises(DomainError) as denied:
        enqueue(notification_state)
    assert denied.value.code == (
        "IDENTITY_AMBIGUOUS" if wrong == "ambiguous" else "RECIPIENT_UNAVAILABLE"
    )
    assert counts(sessions) == (0, 0) and not transport.sent


@pytest.mark.parametrize("changed", ["subject", "suspended", "content", "template"])
def test_dispatch_rechecks_original_recipient_template_and_snapshot_before_io(
    notification_state, changed
):
    service, dispatcher, sessions, ids, template, transport, clock, _ = notification_state
    enqueue(notification_state)
    with sessions.begin() as session:
        if changed == "subject":
            session.scalar(
                select(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"])
            ).external_id = "U_CHANGED"
        elif changed == "suspended":
            session.get(User, ids["alice"]).status = "suspended"
        elif changed == "content":
            session.scalar(select(NotificationOutbox)).content = {
                "title": "Different",
                "summary": "Altered",
            }
    if changed == "template":
        template.definition = template.definition.model_copy(update={"revision": "2"})
        service = NotificationService(sessions, NAMESPACE, [template], clock=lambda: clock["now"])
        dispatcher = NotificationDispatcher(service, LIMITS, transport)
    result = dispatcher.run_once()
    assert result.state == "failed" and not transport.sent
    assert result.code in {
        "RECIPIENT_CHANGED",
        "RECIPIENT_UNAVAILABLE",
        "NOTIFICATION_CHANGED",
        "TEMPLATE_CHANGED",
    }
    assert dispatcher.run_once() is None


def test_explicit_rate_limit_retries_same_snapshot_after_delay(notification_state):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    transport.error = NotificationDeliveryError("RATE_LIMITED", retryable=True, retry_after=10)
    assert dispatcher.run_once().state == "retry"
    clock["now"] += timedelta(seconds=9)
    assert dispatcher.run_once() is None
    clock["now"] += timedelta(seconds=1)
    transport.error = None
    assert dispatcher.run_once().state == "delivered"
    assert len(transport.sent) == 2 and counts(sessions) == (1, 2)
    assert transport.sent[0].notification_id == transport.sent[1].notification_id
    assert transport.sent[0].content == transport.sent[1].content
    assert transport.sent[0].attempt_id != transport.sent[1].attempt_id


@pytest.mark.parametrize("unknown", [False, True])
def test_permanent_rejection_and_uncertain_delivery_remain_distinct_without_auto_resend(
    notification_state, unknown
):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    transport.error = NotificationDeliveryError("FIXTURE_FAILURE", outcome_unknown=unknown)
    result = dispatcher.run_once()
    assert result.state == ("uncertain" if unknown else "failed")
    clock["now"] += timedelta(days=1)
    assert dispatcher.run_once() is None and len(transport.sent) == 1
    with sessions() as session:
        attempt = session.scalar(select(NotificationAttempt))
        assert attempt.state == ("uncertain" if unknown else "rejected")
        assert attempt.channel_ref is None and attempt.message_ref is None


def test_unknown_transport_exception_does_not_pretend_delivery_failed_safely(notification_state):
    _, dispatcher, _, _, _, transport, _, _ = notification_state
    enqueue(notification_state)
    transport.error = RuntimeError("private-transport-payload")
    result = dispatcher.run_once()
    assert result.state == "uncertain" and result.code == "DELIVERY_UNCERTAIN"
    assert "private-transport" not in repr(result)


def test_expired_claim_before_prepare_is_abandoned_and_safe_to_recover(notification_state):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    old = dispatcher.claim()
    clock["now"] = old.lease_expires_at
    assert dispatcher.run_once().state == "delivered" and len(transport.sent) == 1
    assert dispatcher.prepare(old).state == "lost_claim"
    assert dispatcher.finish(old, receipt=RECEIPT).state == "lost_claim"
    with sessions() as session:
        assert session.get(NotificationAttempt, str(old.attempt_id)).state == "abandoned"
        assert session.scalar(select(NotificationOutbox)).attempts == 2


def test_prepared_send_cannot_be_prepared_twice_and_expiry_is_uncertain(notification_state):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    claim = dispatcher.claim()
    prepared = dispatcher.prepare(claim)
    assert prepared.notification_id == claim.notification_id
    assert dispatcher.prepare(claim).state == "lost_claim"
    clock["now"] = claim.lease_expires_at
    assert dispatcher.run_once().state == "uncertain"
    assert dispatcher.run_once() is None and not transport.sent
    assert counts(sessions) == (1, 1)


def test_positive_late_receipt_resolves_original_uncertainty_and_is_idempotent(notification_state):
    _, dispatcher, sessions, _, _, _, clock, _ = notification_state
    enqueue(notification_state)
    claim = dispatcher.claim()
    dispatcher.prepare(claim)
    clock["now"] = claim.lease_expires_at
    assert dispatcher.run_once().state == "uncertain"
    assert dispatcher.finish(claim, receipt=RECEIPT).state == "delivered"
    with sessions() as session:
        completed = session.scalar(select(NotificationOutbox.completed_at))
        audit_count = session.scalar(select(func.count()).select_from(AuditEntry))
    clock["now"] += timedelta(seconds=10)
    assert dispatcher.finish(claim, receipt=RECEIPT).state == "delivered"
    with sessions() as session:
        assert session.scalar(select(NotificationOutbox.completed_at)) == completed
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == audit_count
    with pytest.raises(DomainError, match="receipt differs"):
        dispatcher.finish(claim, receipt=RECEIPT.model_copy(update={"message_ref": "other"}))


def test_old_attempt_receipt_cannot_override_a_subsequent_attempt(notification_state):
    _, dispatcher, sessions, _, _, _, clock, _ = notification_state
    enqueue(notification_state)
    old = dispatcher.claim()
    dispatcher.prepare(old)
    clock["now"] = old.lease_expires_at
    assert dispatcher.run_once().state == "uncertain"
    # Model a future explicit operator resend; no automatic resend API exists.
    with sessions.begin() as session:
        record = session.scalar(select(NotificationOutbox))
        record.state, record.completed_at = "queued", None
    new = dispatcher.claim()
    assert new.attempt == 2
    assert dispatcher.finish(old, receipt=RECEIPT).state == "lost_claim"
    with sessions() as session:
        record = session.scalar(select(NotificationOutbox))
        assert record.last_attempt_id == str(new.attempt_id) and record.state == "running"
        assert session.get(NotificationAttempt, str(old.attempt_id)).state == "late_delivered"
        late = session.scalar(
            select(AuditEntry).where(AuditEntry.action == "notification.attempt.late_delivered")
        )
        assert late.changes["attempt_id"] == str(old.attempt_id) and late.changes["attempt"] == 1


def test_uncertain_result_replay_preserves_original_timestamp_and_audit(notification_state):
    _, dispatcher, sessions, _, _, _, clock, _ = notification_state
    enqueue(notification_state)
    claim = dispatcher.claim()
    dispatcher.prepare(claim)
    error = NotificationDeliveryError("UNKNOWN_DELIVERY", outcome_unknown=True)
    assert dispatcher.finish(claim, error=error).state == "uncertain"
    with sessions() as session:
        completed = session.scalar(select(NotificationOutbox.completed_at))
        before = session.scalar(select(func.count()).select_from(AuditEntry))
    clock["now"] += timedelta(seconds=30)
    assert dispatcher.finish(claim, error=error).state == "uncertain"
    with sessions() as session:
        assert session.scalar(select(NotificationOutbox.completed_at)) == completed
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == before


def test_definite_rejections_have_bounded_attempts(notification_state):
    _, dispatcher, sessions, _, _, transport, clock, _ = notification_state
    enqueue(notification_state)
    transport.error = NotificationDeliveryError("RATE_LIMITED", retryable=True)
    for attempt in range(1, 4):
        result = dispatcher.run_once()
        assert result.state == ("failed" if attempt == 3 else "retry")
        with sessions() as session:
            record = session.scalar(select(NotificationOutbox))
            assert record.attempts == attempt
            clock["now"] = utc(record.available_at)
    assert dispatcher.run_once() is None and len(transport.sent) == 3


def test_event_consumer_inbox_and_notification_enqueue_commit_together(notification_state):
    service, dispatcher, sessions, ids, _, transport, clock, relay_state = notification_state

    class NotifyingConsumer(ProfileConsumer):
        def consume(self, session, envelope):
            super().consume(session, envelope)
            service.enqueue(
                session,
                NotificationIntent(
                    event_id=envelope.event_id,
                    recipient_user_id=ids["alice"],
                    template_id="fixture.notice",
                    parameters={"text": "Fixture event recorded"},
                    request_id="fixture-consumer",
                ),
            )
            if self.after_enqueue_fail:
                raise RuntimeError("fixture-failure-after-enqueue")

    consumer = NotifyingConsumer()
    consumer.after_enqueue_fail = True
    relay = EventRelay(sessions, [consumer], RELAY_LIMITS, clock=lambda: clock["now"])
    seed(relay_state)
    relay.publish_once()
    assert relay.run_once().state == "dead" and counts(sessions) == (0, 0)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConsumerInbox)) == 0
        assert session.get(User, ids["alice"]).display_name == "Original A"
    consumer.after_enqueue_fail = False
    seed(relay_state)
    relay.publish_once()
    assert relay.run_once().state == "processed" and counts(sessions) == (1, 0)
    assert not transport.sent
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ConsumerInbox)) == 1
    assert dispatcher.run_once().state == "delivered"


def test_notification_migration_round_trip_and_nonempty_history_guard(notification_state):
    _, _, _, _, _, _, _, relay_state = notification_state
    seed(relay_state)
    config = Config("alembic.ini")
    command.downgrade(config, "0012")
    command.upgrade(config, "head")
    command.check(config)
    enqueue(notification_state)
    with pytest.raises(RuntimeError, match="notification delivery history prevents downgrade"):
        command.downgrade(config, "0012")
    command.check(config)


@pytest.mark.parametrize(
    "invalid", ["missing_attempt", "sending_without_prepare", "delivered_without_attempt"]
)
def test_database_constraints_reject_missing_send_phase_or_delivery_evidence(
    notification_state, invalid
):
    _, dispatcher, sessions, _, _, _, clock, _ = notification_state
    enqueue(notification_state)
    if invalid != "delivered_without_attempt":
        dispatcher.claim()
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            if invalid == "sending_without_prepare":
                session.scalar(select(NotificationAttempt)).state = "sending"
            else:
                record = session.scalar(select(NotificationOutbox))
                if invalid == "missing_attempt":
                    record.last_attempt_id = None
                else:
                    record.state, record.completed_at = "delivered", clock["now"]
