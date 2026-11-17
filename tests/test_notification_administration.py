from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from aris.core.access.models import (
    ExternalIdentity,
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessScope
from aris.core.audit import AuditEntry
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.schemas import CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.notifications.administration import (
    MANAGE_NOTIFICATIONS,
    MANUAL_RETRY,
    NotificationAdminContext,
    NotificationAdministrationService,
    RetryUncertainNotification,
)
from aris.core.notifications.commands import RetryUncertainNotificationCommand
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.schemas import NotificationDeliveryError
from aris.core.notifications.service import NotificationService
from tests.test_command_intake import KEY, NAMESPACE, WORKER_LIMITS, actor, context
from tests.test_command_intake import LIMITS as INTAKE_LIMITS
from tests.test_notifications import LIMITS, RECEIPT, FixtureTemplate, enqueue
from tests.test_notifications import command_state as command_state
from tests.test_notifications import durable_state as durable_state
from tests.test_notifications import notification_state as notification_state
from tests.test_notifications import relay_state as relay_state


@pytest.fixture
def notification_admin(notification_state):
    service, _, sessions, ids, _, _, _, _ = notification_state
    role, permission, binding = (str(uuid4()) for _ in range(3))
    with sessions.begin() as session:
        session.add_all(
            [
                Role(id=role, name="fixture-notification-operator"),
                Permission(id=permission, name=MANAGE_NOTIFICATIONS),
            ]
        )
        session.flush()
        session.add(RolePermission(role_id=role, permission_id=permission))
        session.add(
            RoleBinding(
                id=binding, user_id=ids["bob"], role_id=role, scope_type="global", scope_id="*"
            )
        )
    return NotificationAdministrationService(service, LIMITS), notification_state, binding


def operation(state):
    return NotificationAdminContext(
        actor_id=state[3]["bob"], operation_id=uuid4(), request_id="fixture-manual-notice-retry"
    )


def uncertain(state):
    handle = enqueue(state)
    dispatcher = state[1]
    claim = dispatcher.claim()
    dispatcher.prepare(claim)
    assert (
        dispatcher.finish(
            claim, error=NotificationDeliveryError("FIXTURE_UNKNOWN", outcome_unknown=True)
        ).state
        == "uncertain"
    )
    request = RetryUncertainNotification(
        notification_id=handle.notification_id,
        expected_attempt_id=claim.attempt_id,
        reason="Operator verified duplicate risk",
        acknowledge_possible_duplicate=True,
    )
    return handle, claim, request


def rows(sessions, model):
    with sessions() as session:
        return tuple(
            tuple(row)
            for row in session.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            )
        )


def all_rows(sessions):
    return tuple(
        rows(sessions, model)
        for model in (NotificationOutbox, NotificationAttempt, AuditEntry, DomainEvent, OutboxEntry)
    )


def test_manual_retry_is_audited_atomic_and_preserves_original_uncertain_evidence(
    notification_admin,
):
    admin, state, _ = notification_admin
    _, dispatcher, sessions, ids, _, transport, _, _ = state
    handle, _, request = uncertain(state)
    old_attempt = rows(sessions, NotificationAttempt)
    result = admin.retry(operation(state), request)
    assert result.state == "queued" and result.duplicate_delivery_possible
    assert result.total_attempts == 1 and rows(sessions, NotificationAttempt) == old_attempt
    with sessions() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "queued" and record.attempts == 1
        assert record.last_error_code == MANUAL_RETRY and record.completed_at is None
        audit = session.scalar(
            select(AuditEntry).where(AuditEntry.action == "notification.manual_retry.approved")
        )
        assert audit.actor == ids["bob"] and audit.changes["reason"] == request.reason
        event = session.scalar(
            select(DomainEvent).where(DomainEvent.event_type == "core.notification.retry_requested")
        )
        assert session.get(OutboxEntry, event.event_id) is not None
        assert event.payload == {
            "previous_attempt_id": str(request.expected_attempt_id),
            "duplicate_delivery_possible": True,
        }
    assert not transport.sent
    assert dispatcher.run_once().state == "delivered" and len(transport.sent) == 1
    with sessions() as session:
        assert session.get(NotificationOutbox, str(handle.notification_id)).attempts == 2
        assert (
            session.get(NotificationAttempt, str(request.expected_attempt_id)).state == "uncertain"
        )


@pytest.mark.parametrize("ack", [False, 1, "true", None])
def test_duplicate_risk_acknowledgement_must_be_explicit_boolean_true(ack):
    with pytest.raises(ValidationError):
        RetryUncertainNotification(
            notification_id=uuid4(),
            expected_attempt_id=uuid4(),
            reason="Review",
            acknowledge_possible_duplicate=ack,
        )


@pytest.mark.parametrize("reason", ["", "  ", "x" * 257])
def test_retry_reason_is_required_and_bounded(reason):
    with pytest.raises(ValidationError):
        RetryUncertainNotification(
            notification_id=uuid4(),
            expected_attempt_id=uuid4(),
            reason=reason,
            acknowledge_possible_duplicate=True,
        )


@pytest.mark.parametrize("change", ["ungranted", "node_scope", "revoked", "suspended"])
def test_manual_retry_requires_current_active_global_notification_permission(
    notification_admin, change
):
    admin, state, binding = notification_admin
    _, _, request = uncertain(state)
    sessions = state[2]
    actor_context = operation(state)
    with sessions.begin() as session:
        if change == "ungranted":
            actor_context = actor_context.model_copy(update={"actor_id": state[3]["alice"]})
        elif change == "node_scope":
            record = session.get(RoleBinding, binding)
            record.scope_type, record.scope_id = "node", "node-a"
        elif change == "revoked":
            session.delete(session.get(RoleBinding, binding))
        else:
            session.get(User, state[3]["bob"]).status = "suspended"
    before = all_rows(sessions)
    with pytest.raises(DomainError) as rejected:
        admin.retry(actor_context, request)
    assert rejected.value.code == "PERMISSION_DENIED" and all_rows(sessions) == before


@pytest.mark.parametrize(
    "change,code",
    [
        ("attempt", "CONFLICT"),
        ("budget", "RETRY_BUDGET_EXHAUSTED"),
        ("content", "NOTIFICATION_CHANGED"),
        ("template", "TEMPLATE_CHANGED"),
        ("recipient", "RECIPIENT_CHANGED"),
        ("inactive_recipient", "RECIPIENT_UNAVAILABLE"),
        ("namespace", "NOT_FOUND"),
        ("unregistered_template", "NOT_FOUND"),
    ],
)
def test_manual_retry_rechecks_evidence_scope_snapshot_recipient_and_budget(
    notification_admin, change, code
):
    admin, state, _ = notification_admin
    _, _, request = uncertain(state)
    service, _, sessions, ids, _, _, clock, _ = state
    if change == "attempt":
        request = request.model_copy(update={"expected_attempt_id": uuid4()})
    elif change == "budget":
        admin = NotificationAdministrationService(
            service, LIMITS.model_copy(update={"max_attempts": 1})
        )
    elif change in {"namespace", "template", "unregistered_template"}:
        namespace = (
            NAMESPACE.model_copy(update={"namespace": "TOTHER"})
            if change == "namespace"
            else NAMESPACE
        )
        template = FixtureTemplate()
        if change == "template":
            template.definition = template.definition.model_copy(update={"revision": "2"})
        service = NotificationService(
            sessions,
            namespace,
            [] if change == "unregistered_template" else [template],
            clock=lambda: clock["now"],
        )
        admin = NotificationAdministrationService(service, LIMITS)
    else:
        with sessions.begin() as session:
            if change == "content":
                session.get(NotificationOutbox, str(request.notification_id)).content = {
                    "title": "Changed",
                    "summary": "Different",
                }
            elif change == "recipient":
                session.scalar(
                    select(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"])
                ).external_id = "U_CHANGED"
            else:
                session.get(User, ids["alice"]).status = "suspended"
    before = all_rows(sessions)
    with pytest.raises(DomainError) as rejected:
        admin.retry(operation(state), request)
    assert rejected.value.code == code and all_rows(sessions) == before


def test_manual_retry_respects_outer_rollback_and_cannot_be_reapplied(notification_admin):
    admin, state, _ = notification_admin
    _, _, request = uncertain(state)
    sessions = state[2]
    before = all_rows(sessions)
    with pytest.raises(RuntimeError):
        with sessions.begin() as session:
            admin.retry_in_transaction(session, operation(state), request)
            raise RuntimeError("fixture transaction owner rolled back")
    assert all_rows(sessions) == before
    admin.retry(operation(state), request)
    after = all_rows(sessions)
    with pytest.raises(DomainError) as conflict:
        admin.retry(operation(state), request)
    assert conflict.value.code == "CONFLICT" and all_rows(sessions) == after


def test_exact_old_success_before_a_new_claim_cancels_manual_resend(notification_admin):
    admin, state, _ = notification_admin
    handle, claim, request = uncertain(state)
    admin.retry(operation(state), request)
    assert state[1].finish(claim, receipt=RECEIPT).state == "delivered"
    assert state[1].run_once() is None and not state[5].sent
    with state[2]() as session:
        assert session.get(NotificationOutbox, str(handle.notification_id)).attempts == 1
        assert session.get(NotificationAttempt, str(claim.attempt_id)).state == "delivered"


def test_repeated_unknown_old_result_does_not_cancel_or_delay_manual_retry(notification_admin):
    admin, state, _ = notification_admin
    _, claim, request = uncertain(state)
    admin.retry(operation(state), request)
    before = all_rows(state[2])
    assert (
        state[1]
        .finish(claim, error=NotificationDeliveryError("FIXTURE_UNKNOWN", outcome_unknown=True))
        .state
        == "lost_claim"
    )
    assert all_rows(state[2]) == before
    assert state[1].run_once().state == "delivered"


def test_late_definite_rejection_retains_evidence_without_cancelling_approved_retry(
    notification_admin,
):
    admin, state, _ = notification_admin
    handle, claim, request = uncertain(state)
    admin.retry(operation(state), request)
    assert (
        state[1].finish(claim, error=NotificationDeliveryError("DEFINITE_REJECTION")).state
        == "lost_claim"
    )
    with state[2]() as session:
        assert session.get(NotificationAttempt, str(claim.attempt_id)).state == "rejected"
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "queued" and record.last_error_code == MANUAL_RETRY
    assert state[1].run_once().state == "delivered"


def test_old_success_after_a_new_claim_only_updates_old_attempt_evidence(notification_admin):
    admin, state, _ = notification_admin
    handle, old, request = uncertain(state)
    admin.retry(operation(state), request)
    newer = state[1].claim()
    assert state[1].finish(old, receipt=RECEIPT).state == "lost_claim"
    with state[2]() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "running" and record.last_attempt_id == str(newer.attempt_id)
        assert session.get(NotificationAttempt, str(old.attempt_id)).state == "late_delivered"
    state[1].prepare(newer)
    assert (
        state[1]
        .finish(newer, receipt=RECEIPT.model_copy(update={"message_ref": "1731800000.000002"}))
        .state
        == "delivered"
    )


def test_confirmed_persistent_command_retries_once_and_replay_does_not_requeue(notification_admin):
    admin, state, _ = notification_admin
    handle, _, request = uncertain(state)
    _, dispatcher, sessions, ids, _, _, clock, _ = state
    commands = CommandService(
        sessions,
        [RetryUncertainNotificationCommand(admin)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    intake = CommandIntakeService(
        commands, NAMESPACE, INTAKE_LIMITS, confirmation_key_id="fixture-key"
    )
    command = CommandRequest(
        command="notification.retry_uncertain",
        scope=AccessScope(type="global", id="*"),
        parameters=request.model_dump(mode="json"),
    )
    submitted = intake.submit(context(ids, user="bob"), command)
    assert submitted.outcome.state == "awaiting_confirmation"
    assert dispatcher.run_once() is None
    confirmation = ConfirmCommand(
        command_id=submitted.outcome.command_id, proof=submitted.outcome.proof
    )
    assert intake.confirm(actor(ids, user="bob"), confirmation).outcome.state == "queued"
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    assert dispatcher.run_once().state == "delivered"
    replay = intake.submit(context(ids, user="bob", receipt="same-intent-again"), command)
    assert replay.outcome.duplicate and replay.outcome.state == "succeeded"
    assert replay.outcome.command_id == submitted.outcome.command_id
    assert intake.submit(context(ids, user="bob"), command).duplicate
    assert (
        replay.outcome.result["state"] == "queued"
        and replay.outcome.result["duplicate_delivery_possible"] is True
    )
    assert dispatcher.run_once() is None
    with sessions() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "delivered" and record.attempts == 2
        assert (
            len(
                session.scalars(
                    select(AuditEntry).where(
                        AuditEntry.action == "notification.manual_retry.approved"
                    )
                ).all()
            )
            == 1
        )


def test_postgres_concurrent_manual_retries_only_approve_one_extra_attempt(notification_admin):
    admin, state, _ = notification_admin
    if state[2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock and concurrent manual retry")
    _, _, request = uncertain(state)
    barrier = Barrier(2)

    def retry():
        barrier.wait(timeout=5)
        try:
            return admin.retry(operation(state), request).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: retry(), range(2))) == ["CONFLICT", "queued"]
    with state[2]() as session:
        assert (
            len(
                session.scalars(
                    select(AuditEntry).where(
                        AuditEntry.action == "notification.manual_retry.approved"
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("state_name", ["queued", "running", "delivered", "failed"])
def test_only_uncertain_state_accepts_manual_retry(notification_admin, state_name):
    admin, state, _ = notification_admin
    handle = enqueue(state)
    claim = None
    if state_name != "queued":
        claim = state[1].claim()
        state[1].prepare(claim)
        if state_name == "delivered":
            state[1].finish(claim, receipt=RECEIPT)
        elif state_name == "failed":
            state[1].finish(claim, error=NotificationDeliveryError("DEFINITE_REJECTION"))
    request = RetryUncertainNotification(
        notification_id=handle.notification_id,
        expected_attempt_id=claim.attempt_id if claim is not None else uuid4(),
        reason="Review original evidence",
        acknowledge_possible_duplicate=True,
    )
    before = all_rows(state[2])
    with pytest.raises(DomainError) as rejected:
        admin.retry(operation(state), request)
    assert rejected.value.code == "CONFLICT" and all_rows(state[2]) == before


@pytest.mark.parametrize(
    "field", ["recipient_user_id", "template_id", "content", "scope", "max_attempts"]
)
def test_manual_retry_does_not_accept_target_content_or_budget_overrides(field):
    data = {
        "notification_id": str(uuid4()),
        "expected_attempt_id": str(uuid4()),
        "reason": "Review",
        "acknowledge_possible_duplicate": True,
        field: "injected",
    }
    with pytest.raises(ValidationError):
        RetryUncertainNotification.model_validate(data)


def test_queued_manual_retry_command_rechecks_current_permission_before_execution(
    notification_admin,
):
    admin, state, binding = notification_admin
    handle, _, request = uncertain(state)
    _, _, sessions, ids, _, transport, clock, _ = state
    commands = CommandService(
        sessions,
        [RetryUncertainNotificationCommand(admin)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    intake = CommandIntakeService(
        commands, NAMESPACE, INTAKE_LIMITS, confirmation_key_id="fixture-key"
    )
    pending = intake.submit(
        context(ids, user="bob"),
        CommandRequest(
            command="notification.retry_uncertain",
            scope=AccessScope(type="global", id="*"),
            parameters=request.model_dump(mode="json"),
        ),
    )
    assert (
        intake.confirm(
            actor(ids, user="bob"),
            ConfirmCommand(
                command_id=pending.outcome.command_id,
                proof=pending.outcome.proof,
            ),
        ).outcome.state
        == "queued"
    )
    with sessions.begin() as session:
        session.delete(session.get(RoleBinding, binding))
    result = CommandWorker(intake, WORKER_LIMITS).run_once()
    assert result.state == "failed" and result.code == "PERMISSION_DENIED"
    with sessions() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "uncertain" and record.attempts == 1
        assert not session.scalars(
            select(AuditEntry).where(AuditEntry.action == "notification.manual_retry.approved")
        ).all()
    assert not transport.sent


def test_postgres_late_success_racing_manual_approval_never_leaves_a_resend_queued(
    notification_admin,
):
    admin, state, _ = notification_admin
    if state[2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row lock across receipt and manual approval")
    handle, claim, request = uncertain(state)
    barrier = Barrier(2)

    def retry():
        barrier.wait(timeout=5)
        try:
            return admin.retry(operation(state), request).state
        except DomainError as error:
            return error.code

    def receive():
        barrier.wait(timeout=5)
        return state[1].finish(claim, receipt=RECEIPT).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        retry_result = pool.submit(retry)
        receipt_result = pool.submit(receive)
        assert retry_result.result(timeout=5) in {"queued", "CONFLICT"}
        assert receipt_result.result(timeout=5) == "delivered"
    assert state[1].run_once() is None and not state[5].sent
    with state[2]() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        assert record.state == "delivered" and record.attempts == 1
