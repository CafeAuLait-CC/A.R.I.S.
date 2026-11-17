import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, event, func, select, text

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.access.schemas import AccessScope, ExternalSubject
from aris.core.audit import AuditEntry
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.models import (
    CommandAdmission,
    CommandIntakeReceipt,
    CommandJob,
    CommandRecord,
)
from aris.core.commands.queue_schemas import (
    CommandIntakeContext,
    CommandWorkerLimits,
    DurableActorContext,
    DurableCommandContext,
    IntakeLimits,
    WithdrawCommand,
)
from aris.core.commands.schemas import CommandDefinition, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from aris.core.contracts import DomainError
from aris.core.time import utc
from tests.test_commands import command_state as command_state
from tests.test_commands import request

KEY = SecretStr("fixture-durable-confirmation-material-123456789")
NAMESPACE = IdentityNamespace(provider="slack", namespace="T_FIXTURE")
LIMITS = IntakeLimits(
    lock_timeout_ms=100,
    statement_timeout_ms=300,
    transaction_timeout_ms=700,
    max_queue_delay_seconds=120,
    max_pending_commands=10,
)
WORKER_LIMITS = CommandWorkerLimits(
    lock_timeout_ms=200,
    statement_timeout_ms=500,
    transaction_timeout_ms=1000,
    lease_seconds=5,
    max_attempts=3,
    retry_base_seconds=1,
)


def subject(user="alice"):
    return ExternalSubject(provider="slack", namespace="T_FIXTURE", external_id="U_" + user)


def context(ids, *, user="alice", key="intent", receipt="submit", **changes):
    return DurableCommandContext(
        actor_id=ids[user],
        subject=subject(user),
        request_id="request-" + receipt,
        receipt_key=receipt,
        idempotency_key=key,
        **changes,
    )


def actor(ids, *, user="alice", receipt="confirm"):
    return CommandIntakeContext(
        actor_id=ids[user],
        subject=subject(user),
        request_id="request-" + receipt,
        receipt_key=receipt,
    )


@pytest.fixture
def durable_state(command_state):
    _, sessions, ids, action = command_state
    clock = {"now": datetime(2026, 11, 17, tzinfo=UTC)}
    with sessions.begin() as session:
        for name in ("alice", "bob"):
            session.add(
                ExternalIdentity(id=str(uuid4()), user_id=ids[name], **subject(name).model_dump())
            )
    action.definition = CommandDefinition(
        name="profile.rename", required_permission="profile.write"
    )
    commands = CommandService(sessions, [action], confirmation_key=KEY, clock=lambda: clock["now"])
    intake = CommandIntakeService(commands, NAMESPACE, LIMITS, confirmation_key_id="fixture-key-1")
    return intake, sessions, ids, action, clock


def queued(state, *, key="intent"):
    intake, _, ids, _, _ = state
    pending = intake.submit(context(ids, key=key, receipt="submit-" + key), request())
    assert pending.disposition == "accepted", pending
    assert pending.outcome.state == "awaiting_confirmation"
    accepted = intake.confirm(
        actor(ids, receipt="confirm-" + key),
        ConfirmCommand(command_id=pending.outcome.command_id, proof=pending.outcome.proof),
    )
    assert accepted.disposition == "accepted", accepted
    assert accepted.outcome.state == "queued"
    return pending, accepted


def stored_counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (CommandRecord, CommandAdmission, CommandJob, CommandIntakeReceipt)
        )


def restarted(intake, action, clock):
    commands = CommandService(
        intake.sessions,
        [action],
        confirmation_key=intake.commands.confirmation_key,
        clock=lambda: clock["now"],
    )
    return CommandIntakeService(
        commands,
        intake.namespace,
        intake.limits,
        confirmation_key_id=intake.confirmation_key_id,
    )


def test_durable_confirmation_commits_receipts_then_worker_executes_once(durable_state):
    intake, sessions, ids, action, clock = durable_state
    pending, accepted = queued(durable_state)
    assert action.calls == 0 and stored_counts(sessions) == (1, 1, 1, 2)
    assert pending.outcome.execution_not_after == clock["now"] + timedelta(seconds=420)
    assert accepted.outcome.execute_by == clock["now"] + timedelta(seconds=120)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        record = session.get(CommandRecord, str(accepted.outcome.command_id))
        assert record.state == "queued" and record.result is None
        assert session.scalar(select(CommandAdmission)).verification_kind == "proof"
    worker = CommandWorker(intake, WORKER_LIMITS)
    assert worker.run_once().state == "succeeded"
    assert worker.run_once() is None
    result = intake.inspect(actor(ids), accepted.outcome.command_id)
    assert result.state == "succeeded" and result.result == {"name": "Renamed"}
    assert result.duplicate and action.calls == 1
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Renamed"
        assert session.scalar(select(CommandJob)).state == "succeeded"
        audit = list(session.scalars(select(AuditEntry)))
        stored = json.dumps([item.changes for item in audit])
        stored += json.dumps(session.scalar(select(CommandAdmission)).__dict__, default=str)
        assert pending.outcome.proof.get_secret_value() not in stored
        assert KEY.get_secret_value() not in stored
        assert "command.succeeded" in [item.action for item in audit]


def test_nonconfirmation_still_queues_without_executing_at_admission(durable_state):
    intake, sessions, ids, action, clock = durable_state
    action.definition = action.definition.model_copy(update={"requires_confirmation": False})
    intake = restarted(intake, action, clock)
    accepted = intake.submit(context(ids), request())
    assert accepted.outcome.state == "queued" and action.calls == 0
    with sessions() as session:
        assert session.scalar(select(CommandAdmission)).verification_kind == "not_required"
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    assert action.calls == 1


def test_replays_preserve_original_intent_and_execution_deadlines(durable_state):
    intake, sessions, ids, action, clock = durable_state
    first = intake.submit(context(ids), request("  Renamed  "))
    clock["now"] += timedelta(seconds=60)
    for receipt in ("submit", "submit-again"):
        replay = intake.submit(context(ids, receipt=receipt), request())
        assert replay.outcome == first.outcome and action.calls == 0
    assert replay.duplicate is False  # Different receipt, same business intent.
    proof = ConfirmCommand(command_id=first.outcome.command_id, proof=first.outcome.proof)
    accepted = intake.confirm(actor(ids), proof)
    clock["now"] += timedelta(seconds=30)
    for receipt in ("confirm", "confirm-again"):
        replay = intake.confirm(actor(ids, receipt=receipt), proof)
        assert replay.outcome.execute_by == accepted.outcome.execute_by
    assert stored_counts(sessions) == (1, 1, 1, 4)


@pytest.mark.parametrize("same_receipt", [False, True])
def test_conflicting_receipt_or_business_key_is_rejected_without_replacing_intent(
    durable_state, same_receipt
):
    intake, sessions, ids, action, clock = durable_state
    first = intake.submit(context(ids), request())
    result = intake.submit(
        context(ids, receipt="submit" if same_receipt else "different"), request("Changed")
    )
    assert result.error_code == "CONFLICT" and result.disposition == "rejected"
    assert action.calls == 0
    assert intake.inspect(actor(ids), first.outcome.command_id) == first.outcome
    with sessions() as session:
        assert session.get(CommandRecord, str(first.outcome.command_id)).parameters == {
            "name": "Renamed"
        }


@pytest.mark.parametrize("wrong", ["unknown", "actor", "namespace", "suspended"])
def test_untrusted_identity_is_rejected_and_audited_without_enrollment(durable_state, wrong):
    intake, sessions, ids, action, _ = durable_state
    ctx = context(ids)
    if wrong == "unknown":
        ctx = ctx.model_copy(update={"actor_id": uuid4()})
    elif wrong == "actor":
        ctx = ctx.model_copy(update={"subject": subject("bob")})
    elif wrong == "namespace":
        ctx = ctx.model_copy(update={"subject": subject().model_copy(update={"namespace": "T_X"})})
    else:
        with sessions.begin() as session:
            session.get(User, ids["alice"]).status = "suspended"
    rejected = intake.submit(ctx, request())
    assert rejected.error_code == "PERMISSION_DENIED" and not rejected.retryable
    assert stored_counts(sessions) == (0, 0, 0, 1) and action.calls == 0
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 2
        assert session.scalar(select(CommandIntakeReceipt)).state == "rejected"


@pytest.mark.parametrize("phase", ["confirm", "worker"])
@pytest.mark.parametrize("revocation", ["role", "subject", "user", "scope", "definition"])
def test_current_identity_permission_scope_and_definition_are_rechecked(
    durable_state, phase, revocation
):
    intake, sessions, ids, action, clock = durable_state
    pending = intake.submit(context(ids), request()).outcome
    proof = ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    if phase == "worker":
        assert intake.confirm(actor(ids), proof).outcome.state == "queued"
    with sessions.begin() as session:
        if revocation == "role":
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        elif revocation == "subject":
            session.execute(
                delete(ExternalIdentity).where(ExternalIdentity.user_id == ids["alice"])
            )
        elif revocation == "user":
            session.get(User, ids["alice"]).status = "suspended"
    if revocation == "scope":
        action.authorization_scope = lambda *_: AccessScope(type="node", id="different")
    elif revocation == "definition":
        action.definition = action.definition.model_copy(update={"requires_confirmation": False})
        intake = restarted(intake, action, clock)
    if phase == "confirm":
        result = intake.confirm(actor(ids), proof)
        assert result.error_code in {"PERMISSION_DENIED", "COMMAND_DEFINITION_CHANGED"}
    else:
        result = CommandWorker(intake, WORKER_LIMITS).run_once()
        assert result.state == "failed"
        assert result.code in {"PERMISSION_DENIED", "COMMAND_DEFINITION_CHANGED"}
    assert action.calls == 0
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"


@pytest.mark.parametrize("reuse_key_id", [False, True])
def test_key_rotation_rejects_unconfirmed_intents_without_resigning(durable_state, reuse_key_id):
    intake, sessions, ids, action, _ = durable_state
    pending = intake.submit(context(ids), request()).outcome
    intake.commands.confirmation_key = SecretStr("different-fixture-material-123456789")
    if not reuse_key_id:
        intake.confirmation_key_id = "fixture-key-2"
    assert intake.submit(context(ids, receipt="new-submit"), request()).error_code == (
        "CONFIRMATION_KEY_CHANGED"
    )
    result = intake.confirm(
        actor(ids), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )
    assert result.error_code == "CONFIRMATION_KEY_CHANGED" and action.calls == 0
    assert stored_counts(sessions) == (1, 1, 0, 3)


def test_verified_consent_survives_worker_key_rotation(durable_state):
    intake, _, _, action, _ = durable_state
    queued(durable_state)
    intake.commands.confirmation_key = SecretStr("rotated-fixture-material-123456789")
    intake.confirmation_key_id = "fixture-key-2"
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    assert action.calls == 1


def test_bad_proof_is_recorded_only_as_a_sanitized_rejection(durable_state):
    intake, sessions, ids, action, _ = durable_state
    pending = intake.submit(context(ids), request()).outcome
    proof = "1" * 64
    result = intake.confirm(actor(ids), ConfirmCommand(command_id=pending.command_id, proof=proof))
    assert result.error_code == "PERMISSION_DENIED" and action.calls == 0
    with sessions() as session:
        rows = list(session.scalars(select(CommandIntakeReceipt)))
        assert proof not in json.dumps([item.__dict__ for item in rows], default=str)
        assert session.scalar(select(CommandAdmission)).verified_at is None


def test_expired_confirmation_cannot_queue_and_queued_timeout_is_not_fake_completion(durable_state):
    intake, _, ids, action, clock = durable_state
    pending = intake.submit(context(ids), request()).outcome
    clock["now"] = pending.expires_at
    expired = intake.confirm(
        actor(ids), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )
    assert expired.outcome.state == "expired"
    _, accepted = queued(durable_state, key="second")
    clock["now"] = accepted.outcome.execute_by
    unchecked = intake.inspect(actor(ids), accepted.outcome.command_id)
    assert unchecked.state == "queued" and unchecked.deadline_elapsed
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "expired"
    assert intake.inspect(actor(ids), accepted.outcome.command_id).state == "expired"
    assert action.calls == 0


@pytest.mark.parametrize("before_confirmation", [False, True])
def test_owner_can_withdraw_unexecuted_consent_even_after_permission_loss(
    durable_state, before_confirmation
):
    intake, sessions, ids, action, _ = durable_state
    if before_confirmation:
        pending = intake.submit(context(ids), request())
    else:
        _, pending = queued(durable_state)
    with sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    withdrawn = intake.withdraw(
        actor(ids, receipt="withdraw"), WithdrawCommand(command_id=pending.outcome.command_id)
    )
    assert withdrawn.outcome.state == "cancelled"
    duplicate = intake.withdraw(
        actor(ids, receipt="withdraw"), WithdrawCommand(command_id=pending.outcome.command_id)
    )
    assert duplicate.duplicate and duplicate.outcome == withdrawn.outcome
    assert CommandWorker(intake, WORKER_LIMITS).run_once() is None and action.calls == 0


def test_withdraw_cannot_undo_executed_business_or_another_owners_intent(durable_state):
    intake, _, ids, _, _ = durable_state
    _, accepted = queued(durable_state)
    target = WithdrawCommand(command_id=accepted.outcome.command_id)
    assert intake.withdraw(actor(ids, user="bob", receipt="bob-withdraw"), target).error_code == (
        "PERMISSION_DENIED"
    )
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    result = intake.withdraw(actor(ids, receipt="withdraw"), target)
    assert result.error_code == "COMMAND_ALREADY_FINISHED"


def test_capacity_rejection_can_retry_same_receipt_without_resetting_old_deadlines(durable_state):
    intake, sessions, ids, action, clock = durable_state
    intake.limits = LIMITS.model_copy(update={"max_pending_commands": 1})
    first = intake.submit(context(ids), request())
    ctx = context(ids, key="second", receipt="second")
    full = intake.submit(ctx, request())
    assert full.error_code == "QUEUE_FULL" and full.retryable
    clock["now"] = first.outcome.expires_at
    retried = intake.submit(ctx, request())
    assert retried.disposition == "accepted" and retried.duplicate and action.calls == 0
    with sessions() as session:
        receipt = session.get(CommandIntakeReceipt, str(retried.receipt_id))
        assert receipt.attempts == 2
        original = session.get(CommandRecord, str(first.outcome.command_id))
        assert utc(original.confirmation_expires_at) == first.outcome.expires_at


@pytest.mark.parametrize("misbehavior", ["orm", "sql", "commit"])
def test_scope_provider_cannot_write_or_commit_during_short_admission(durable_state, misbehavior):
    intake, sessions, ids, action, _ = durable_state

    def invalid_scope(session, parameters):
        if misbehavior == "orm":
            session.get(User, ids["alice"]).display_name = "Illicit"
        elif misbehavior == "sql":
            session.execute(text("UPDATE users SET display_name='Illicit'"))
        else:
            session.commit()
        return AccessScope(type="global", id="*")

    action.authorization_scope = invalid_scope
    result = intake.submit(context(ids), request())
    assert result.error_code == "INTERNAL_ERROR" and action.calls == 0
    assert stored_counts(sessions) == (0, 0, 0, 1)
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"


def test_business_failure_rolls_back_then_persists_separate_failed_job(durable_state):
    intake, sessions, ids, action, _ = durable_state
    _, accepted = queued(durable_state)
    action.fail = True
    result = CommandWorker(intake, WORKER_LIMITS).run_once()
    assert result.state == "failed" and result.code == "INTERNAL_ERROR"
    outcome = intake.inspect(actor(ids), accepted.outcome.command_id)
    assert outcome.state == "failed" and outcome.code == "INTERNAL_ERROR"
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        actions = list(session.scalars(select(AuditEntry.action)))
        assert "command.succeeded" not in actions and "command.failed" in actions
        assert "private-handler-detail" not in json.dumps(
            [row.changes for row in session.scalars(select(AuditEntry))]
        )


@pytest.mark.parametrize("at_flush", [False, True])
def test_deadline_crossing_after_business_work_rolls_everything_back(durable_state, at_flush):
    intake, sessions, ids, action, clock = durable_state
    _, accepted = queued(durable_state)
    deadline = accepted.outcome.execute_by
    clock["now"] = deadline - timedelta(seconds=1)
    original = action.execute

    def delayed(session, execution, principal, parameters):
        result = original(session, execution, principal, parameters)
        if at_flush:

            def expire_after_flush(active, flush_context):
                clock["now"] = deadline

            event.listen(session, "after_flush_postexec", expire_after_flush, once=True)
        else:
            clock["now"] = deadline
        return result

    action.execute = delayed
    result = CommandWorker(intake, WORKER_LIMITS).run_once()
    assert result.state == "expired" and result.code == "EXECUTION_EXPIRED"
    with sessions() as session:
        assert session.get(User, ids["alice"]).display_name == "Original A"
        assert "command.succeeded" not in session.scalars(select(AuditEntry.action)).all()


def test_lease_recovery_fences_old_claim_and_reuses_original_business_key(durable_state):
    intake, sessions, ids, action, clock = durable_state
    queued(durable_state)
    worker = CommandWorker(intake, WORKER_LIMITS)
    first = worker.claim()
    assert worker.claim() is None
    clock["now"] = first.lease_expires_at
    replacement = worker.claim()
    assert replacement.attempt == 2 and replacement.lease_token != first.lease_token
    assert worker.execute(first).state == "lost_claim" and action.calls == 0
    assert worker.execute(replacement).state == "succeeded" and action.calls == 1
    assert worker.execute(first).state == "succeeded" and action.calls == 1
    assert stored_counts(sessions) == (1, 1, 1, 2)


def test_durable_records_cannot_be_executed_through_old_synchronous_entrypoints(durable_state):
    intake, _, ids, action, _ = durable_state
    pending = intake.submit(context(ids), request()).outcome
    from aris.core.commands.schemas import ActorContext, CommandContext

    with pytest.raises(DomainError, match="durable intake"):
        intake.commands.execute(
            CommandContext(actor_id=ids["alice"], request_id="sync", idempotency_key="intent"),
            request(),
        )
    with pytest.raises(DomainError, match="durable confirmation"):
        intake.commands.confirm(
            ActorContext(actor_id=ids["alice"], request_id="sync"),
            ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
        )
    assert action.calls == 0


def test_nonempty_durable_history_blocks_destructive_downgrade(durable_state):
    intake, sessions, ids, _, _ = durable_state
    queued(durable_state)
    with pytest.raises(RuntimeError, match="durable command history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0010")
    command.check(Config("alembic.ini"))
    assert stored_counts(sessions) == (1, 1, 1, 2)


def test_empty_0011_round_trip_preserves_synchronous_result_and_confirmation(durable_state):
    intake, sessions, ids, action, _ = durable_state
    from aris.core.commands.schemas import ActorContext, CommandContext

    def sync_context(key):
        return CommandContext(actor_id=ids["alice"], request_id="sync", idempotency_key=key)

    pending = intake.commands.execute(sync_context("sync-pending"), request())
    second = intake.commands.execute(sync_context("sync-done"), request("Completed"))
    intake.commands.confirm(
        ActorContext(actor_id=ids["alice"], request_id="sync-confirm"),
        ConfirmCommand(command_id=second.command_id, proof=second.proof),
    )

    def snapshot():
        with sessions() as session:
            return [
                tuple(row)
                for row in session.execute(
                    text(
                        "SELECT id,state,fingerprint,parameters,result,confirmation_expires_at "
                        "FROM commands ORDER BY id"
                    )
                )
            ]

    before = snapshot()
    command.downgrade(Config("alembic.ini"), "0010")
    assert snapshot() == before
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    assert snapshot() == before
    again = intake.commands.execute(sync_context("sync-pending"), request())
    assert again == pending
    done = intake.commands.confirm(
        ActorContext(actor_id=ids["alice"], request_id="sync-after-migrate"),
        ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
    )
    assert done.state == "succeeded" and action.calls == 2


@pytest.mark.parametrize("limits", [IntakeLimits, CommandWorkerLimits])
def test_missing_or_misordered_time_budgets_cannot_enable_admission(limits):
    with pytest.raises(ValidationError):
        limits()
    source = LIMITS if limits is IntakeLimits else WORKER_LIMITS
    with pytest.raises(ValidationError):
        limits.model_validate(
            {**source.model_dump(), "lock_timeout_ms": source.transaction_timeout_ms}
        )


def test_fixture_actor_context_excludes_receipt_and_intent_keys_from_repr(durable_state):
    _, _, ids, _, _ = durable_state
    ctx = context(ids, receipt="private-receipt", key="private-business-key")
    assert "private-receipt" not in repr(ctx).split("request_id=")[0]
    assert "private-business-key" not in repr(ctx)
    assert isinstance(actor(ids), DurableActorContext)
