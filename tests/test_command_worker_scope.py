from copy import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.models import CommandAdmission, CommandJob, CommandRecord
from aris.core.commands.schemas import ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from tests.test_command_intake import KEY, LIMITS, NAMESPACE, WORKER_LIMITS, actor, context, queued
from tests.test_command_intake import command_state as command_state
from tests.test_command_intake import durable_state as durable_state
from tests.test_commands import request


def other_worker(state, kind):
    intake, sessions, _, action, clock = state
    namespace = NAMESPACE
    if kind in {"namespace", "provider"}:
        namespace = IdentityNamespace(
            provider="other" if kind == "provider" else "slack",
            namespace=NAMESPACE.namespace if kind == "provider" else "T_OTHER",
        )
    commands = CommandService(
        sessions,
        [] if kind == "registration" else [action],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    other = CommandIntakeService(
        commands, namespace, LIMITS, confirmation_key_id=intake.confirmation_key_id
    )
    return CommandWorker(other, WORKER_LIMITS)


def snapshot(sessions):
    with sessions() as session:
        job = session.scalar(select(CommandJob))
        record = session.scalar(select(CommandRecord))
        return (
            job.state,
            job.attempts,
            job.lease_token,
            job.lease_expires_at,
            job.last_error_code,
            record.state,
            record.failure_code,
        )


@pytest.mark.parametrize("kind", ["registration", "namespace", "provider"])
@pytest.mark.parametrize("phase", ["claim", "execute", "recover"])
def test_worker_never_claims_or_marks_failed_another_handlers_or_namespaces_job(
    durable_state, kind, phase
):
    intake, sessions, _, action, _ = durable_state
    queued(durable_state)
    owner = CommandWorker(intake, WORKER_LIMITS)
    other = other_worker(durable_state, kind)
    if phase == "claim":
        before = snapshot(sessions)
        assert other.claim() is None and snapshot(sessions) == before
        assert owner.run_once().state == "succeeded"
    else:
        claim = owner.claim()
        before = snapshot(sessions)
        result = (
            other.execute(claim) if phase == "execute" else other._recover(claim, "INTERNAL_ERROR")
        )
        assert result.state == "lost_claim" and snapshot(sessions) == before
        assert owner.execute(claim).state == "succeeded"
    assert action.calls == 1


def test_supported_job_is_not_blocked_by_an_older_unregistered_job(durable_state):
    intake, sessions, ids, action, clock = durable_state
    foreign_action = copy(action)
    foreign_action.definition = action.definition.model_copy(update={"name": "profile.other"})
    commands = CommandService(
        sessions, [foreign_action], confirmation_key=KEY, clock=lambda: clock["now"]
    )
    foreign = CommandIntakeService(
        commands, NAMESPACE, LIMITS, confirmation_key_id=intake.confirmation_key_id
    )
    waiting = foreign.submit(
        context(ids, key="other-first", receipt="other-submit"),
        request().model_copy(update={"command": "profile.other"}),
    )
    foreign.confirm(
        actor(ids, receipt="other-confirm"),
        ConfirmCommand(command_id=waiting.outcome.command_id, proof=waiting.outcome.proof),
    )
    clock["now"] += timedelta(seconds=1)
    queued(durable_state)
    assert CommandWorker(intake, WORKER_LIMITS).run_once().state == "succeeded"
    with sessions() as session:
        record = session.get(CommandRecord, str(waiting.outcome.command_id))
        assert record.state == "queued"
    assert CommandWorker(foreign, WORKER_LIMITS).run_once().state == "succeeded"
    assert action.calls == foreign_action.calls == 1


def test_postgres_claim_locks_the_job_without_locking_joined_admission(durable_state):
    intake, sessions, _, action, _ = durable_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL joined claim lock scope")
    queued(durable_state)
    worker = CommandWorker(intake, WORKER_LIMITS)
    with sessions.begin() as held:
        held.execute(select(CommandAdmission).with_for_update()).all()
        claim = worker.claim()
        assert claim is not None
    assert worker.execute(claim).state == "succeeded" and action.calls == 1
