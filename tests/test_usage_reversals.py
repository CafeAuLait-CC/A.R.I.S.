from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, event, update

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission, User
from aris.core.access.schemas import AccessScope
from aris.core.commands.schemas import ActorContext, CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_commands import ReverseUsageMeasurementCommand
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import ReversedMeasurement
from aris.modules.lyra.models import UsageLedgerClaim
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, counts, ledger, postgres

__all__ = ["admin_state", "history_state", "derivations", "ledger"]
NODE = AccessScope(type="node", id="node-a")
KEY = SecretStr("fixture-ledger-reversal-confirmation-key-123456789")


@pytest.fixture
def adjustments(ledger):
    service, request, (_, _, history), _, _ = ledger
    first = service.post(POST, request)
    entry = service.exact(first.entries[0])
    sessions = history[1]
    owner, other = history[4]
    ids = {name: str(uuid4()) for name in ("role", "permission", "binding", "other_binding")}
    with sessions.begin() as session:
        session.add(Role(id=ids["role"], name="fixture-usage-reviewer"))
        session.add(Permission(id=ids["permission"], name="usage.adjust"))
        session.flush()
        session.add(RolePermission(role_id=ids["role"], permission_id=ids["permission"]))
        for user, binding, node in (
            (owner, "binding", "node-a"),
            (other, "other_binding", "node-b"),
        ):
            session.add(
                RoleBinding(
                    id=ids[binding],
                    user_id=user,
                    role_id=ids["role"],
                    scope_type="node",
                    scope_id=node,
                )
            )
    commands = CommandService(
        sessions,
        [ReverseUsageMeasurementCommand(service)],
        confirmation_key=KEY,
        clock=lambda: history[5]["now"],
    )
    parameters = dict(
        target_id=str(entry.id),
        expected_target_fingerprint=entry.fingerprint,
        expected_target_revision=entry.revision,
        reason="Fixture reviewed attribution correction",
    )
    return ledger, commands, owner, other, ids, parameters, first


def pending(state, *, actor=None, key="fixture-reversal", scope=NODE, changes=None):
    return state[1].execute(
        CommandContext(
            actor_id=actor or state[2], request_id="fixture-review", idempotency_key=key
        ),
        CommandRequest(
            command="usage.reverse_measurement",
            scope=scope,
            parameters={**state[5], **(changes or {})},
        ),
    )


def confirm(state, review, *, actor=None, proof=None):
    return state[1].confirm(
        ActorContext(actor_id=actor or state[2], request_id="fixture-confirm"),
        ConfirmCommand(command_id=review.command_id, proof=proof or review.proof),
    )


def test_confirmation_appends_reversal_and_keeps_original_measurement_and_sources(adjustments):
    state = adjustments
    ledger = state[0]
    original = ledger[0].exact(state[6].entries[0])
    source = ledger[2][0].exact(ledger[1].derivation_id)
    review = pending(state)
    before = counts(ledger)
    assert review.state == "awaiting_confirmation" and before[:4] == (1, 1, 1, 1)
    result = confirm(state, review)
    receipt = result.result
    assert (
        result.state == "succeeded"
        and receipt["effect"] == "reversed"
        and not receipt["quota_usage_available"]
    )
    assert counts(ledger)[:4] == (2, 0, 2, 1)
    entry = ledger[0].exact(receipt["entries"][0])
    assert isinstance(entry.details, ReversedMeasurement)
    assert (
        entry.details.target_id == original.id
        and entry.details.target_fingerprint == original.fingerprint
    )
    assert entry.previous_fingerprint == original.fingerprint and entry.revision == 2
    assert entry.actor == state[2] and entry.details.reason == state[5]["reason"]
    assert ledger[0].exact(original.id) == original
    assert ledger[2][0].exact(ledger[1].derivation_id) == source
    after = counts(ledger)
    replay = confirm(state, review)
    assert replay.duplicate and replay.result == receipt and counts(ledger) == after


def test_permissions_and_actual_target_scope_are_required_even_for_the_owner(adjustments):
    state = adjustments
    before = counts(state[0])
    with pytest.raises(DomainError) as error:
        pending(state, actor=state[3])
    assert error.value.code == "PERMISSION_DENIED"
    with pytest.raises(DomainError):
        pending(state, scope=AccessScope(type="node", id="node-b"))
    with state[0][2][2][1].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.id == state[4]["binding"]))
    with pytest.raises(DomainError):
        pending(state)
    assert counts(state[0]) == before


def test_confirmation_cannot_change_actor_proof_or_reviewed_entry(adjustments):
    state = adjustments
    review = pending(state)
    before = counts(state[0])
    with pytest.raises(DomainError):
        confirm(state, review, actor=state[3])
    with pytest.raises(DomainError):
        confirm(state, review, proof=SecretStr("e" * 64))
    assert counts(state[0]) == before
    changed = pending(
        state, key="fixture-mismatched-proof", changes={"expected_target_fingerprint": "f" * 64}
    )
    with pytest.raises(DomainError) as error:
        confirm(state, changed)
    assert error.value.code == "CONFLICT" and counts(state[0])[:4] == (1, 1, 1, 1)


@pytest.mark.parametrize("change", ["revoke", "suspend"])
def test_confirmation_rechecks_current_permission_and_account_status(adjustments, change):
    state = adjustments
    review = pending(state)
    before = counts(state[0])
    sessions = state[0][2][2][1]
    with sessions.begin() as session:
        if change == "revoke":
            session.execute(delete(RoleBinding).where(RoleBinding.id == state[4]["binding"]))
        else:
            session.execute(update(User).where(User.id == state[2]).values(status="suspended"))
    with pytest.raises(DomainError) as error:
        confirm(state, review)
    assert error.value.code == "PERMISSION_DENIED" and counts(state[0]) == before


def test_paused_recording_does_not_remove_explicit_administrative_correction(adjustments):
    state = adjustments
    state[0][0].configuration = None
    assert confirm(state, pending(state)).result["effect"] == "reversed"


def test_second_operation_cannot_reverse_a_reversal_or_reactivate_voided_time(adjustments):
    state = adjustments
    first = confirm(state, pending(state))
    ledger = state[0]
    before = counts(ledger)
    assert ledger[0].post(POST, ledger[1]).duplicate
    with pytest.raises(DomainError) as error:
        ledger[0].post(POST, ledger[1].model_copy(update={"id": uuid4()}))
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(ledger) == before
    again = pending(state, key="fixture-reverse-again")
    with pytest.raises(DomainError):
        confirm(state, again)
    reversal = ledger[0].exact(first.result["entries"][0])
    invalid = pending(
        state,
        key="fixture-reverse-a-reversal",
        changes={
            "target_id": str(reversal.id),
            "expected_target_fingerprint": reversal.fingerprint,
            "expected_target_revision": reversal.revision,
        },
    )
    with pytest.raises(DomainError) as error:
        confirm(state, invalid)
    assert error.value.code == "VALIDATION_ERROR" and counts(ledger)[:4] == (2, 0, 2, 1)


@pytest.mark.parametrize(
    "table", ["usage_ledger_entries", "usage_ledger_operations", "event_outbox"]
)
def test_reversal_write_failure_keeps_the_original_active_claim(adjustments, table):
    state = adjustments
    review = pending(state)
    before = counts(state[0])
    engine = state[0][2][2][2]

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table):
            raise RuntimeError("fixture reversal write interruption")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            confirm(state, review)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(state[0]) == before
    assert confirm(state, review).result["effect"] == "reversed"


def test_permission_loss_after_entry_write_rolls_back_ledger_and_command_completion(
    adjustments, monkeypatch
):
    state = adjustments
    review = pending(state)
    service = state[0][0]
    authorize = service._authorize_adjustment
    calls = []
    before = counts(state[0])

    def recheck(session, execution, principal, node):
        calls.append(node)
        if len(calls) == 3:
            session.execute(delete(RoleBinding).where(RoleBinding.id == state[4]["binding"]))
        return authorize(session, execution, principal, node)

    monkeypatch.setattr(service, "_authorize_adjustment", recheck)
    with pytest.raises(DomainError) as error:
        confirm(state, review)
    assert error.value.code == "PERMISSION_DENIED" and counts(state[0]) == before
    monkeypatch.setattr(service, "_authorize_adjustment", authorize)
    assert confirm(state, review).result["effect"] == "reversed"


def test_missing_projection_is_not_treated_as_a_successfully_reversed_measurement(adjustments):
    state = adjustments
    review = pending(state)
    sessions = state[0][2][2][1]
    with sessions.begin() as session:
        session.execute(delete(UsageLedgerClaim))
    before = counts(state[0])
    with pytest.raises(DomainError) as error:
        confirm(state, review)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(state[0]) == before


def test_reviewed_target_revision_is_stable_when_unrelated_entries_advance_the_node(adjustments):
    state = adjustments
    review = pending(state)
    # A second half-open source window on the same GPU is a separate entry.
    from tests.test_usage_ledger import post_window

    state[0][0].post(POST, post_window(state[0], start=40, seq=9))
    assert confirm(state, review).result["head"]["revision"] == 3
    assert counts(state[0])[:4] == (3, 1, 3, 1)


def test_postgresql_concurrent_confirmations_of_distinct_reversals_only_void_once(adjustments):
    state = adjustments
    postgres(state[0])
    first = pending(state, key="fixture-first-review")
    second = pending(state, key="fixture-second-review")

    def run(review):
        try:
            return confirm(state, review).result["effect"]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, second)))
    assert sorted(results) == ["USAGE_NEEDS_REVIEW", "reversed"]
    assert counts(state[0])[:4] == (2, 0, 2, 1)


def test_postgresql_waiting_adjustment_rechecks_revocation_before_writing(adjustments, monkeypatch):
    state = adjustments
    sessions = postgres(state[0])
    review = pending(state)
    waiting = Event()
    lock = UsageLedgerRepository.lock
    with sessions.begin() as holder:
        lock(UsageLedgerRepository(holder), "node", "node-a")

        def observed(repo, kind, key):
            if kind == "node":
                waiting.set()
            return lock(repo, kind, key)

        monkeypatch.setattr(UsageLedgerRepository, "lock", observed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(confirm, state, review)
            try:
                assert waiting.wait(timeout=3)
                with sessions.begin() as current:
                    current.execute(
                        delete(RoleBinding).where(RoleBinding.id == state[4]["binding"])
                    )
            finally:
                holder.commit()
            with pytest.raises(DomainError) as error:
                future.result(timeout=3)
    assert error.value.code == "PERMISSION_DENIED" and counts(state[0])[:4] == (1, 1, 1, 1)


def test_reversal_input_cannot_replace_identity_quantity_or_scope(adjustments):
    for name in ("actor_id", "owner_id", "observed_microseconds", "scope", "id"):
        with pytest.raises(DomainError) as error:
            pending(adjustments, changes={name: "forged"})
        assert error.value.code == "VALIDATION_ERROR"
