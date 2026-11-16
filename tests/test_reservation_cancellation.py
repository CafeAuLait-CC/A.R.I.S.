import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, update

from aris.core.access.models import Permission, RoleBinding, RolePermission
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandExecution, CommandRequest
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.modules.nova.commands import CancelReservationCommand, CreateReservationCommand
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import CancelReservation
from aris.modules.vega.models import GpuDevice
from tests.test_gpu_target_facts import NOW
from tests.test_reservation_service import KEY, confirm, context, counts, create, denied
from tests.test_reservation_service import booking as booking


@pytest.fixture
def cancelling(booking):
    service, _, sessions, ids, clock, _ = booking
    permission = str(uuid4())
    with sessions.begin() as session:
        session.add(Permission(id=permission, name="reservation.cancel_own"))
        session.flush()
        session.add(RolePermission(role_id=ids["reader"], permission_id=permission))
    commands = CommandService(
        sessions,
        [CreateReservationCommand(service), CancelReservationCommand(service)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    return service, commands, *booking[2:]


def request(reservation_id, version=1, **changes):
    return CommandRequest(
        command="reservation.cancel",
        scope=AccessScope(type="global", id="*"),
        parameters={
            "reservation_id": str(reservation_id),
            "expected_version": version,
            "reason": "Owner changed their plan",
            **changes,
        },
    )


def pending(state, reservation_id, *, key="cancel-once", user="alice", **changes):
    return state[1].execute(context(state, user, key), request(reservation_id, **changes))


def cancelled(state, reservation_id, **changes):
    return confirm(state, pending(state, reservation_id, **changes))


def history(state):
    with state[2]() as session:
        return [
            (item.version, item.event_type, json.dumps(item.payload, sort_keys=True))
            for item in session.scalars(select(ReservationEvent).order_by(ReservationEvent.version))
        ]


def test_confirmed_owner_cancellation_appends_history_and_returns_original_result_on_retry(
    cancelling,
):
    done = create(cancelling)
    reservation_id = done.result["reservation"]["id"]
    original = history(cancelling)
    waiting = pending(cancelling, reservation_id)
    assert history(cancelling) == original and counts(cancelling) == (1, 1, 1, 1)
    result = confirm(cancelling, waiting)
    assert result.result["status"] == "cancelled" and result.result["version"] == 2
    assert result.result["id"] == reservation_id
    current = history(cancelling)
    assert current[:1] == original
    assert current[1][0:2] == (2, "reservation.cancelled")
    evidence = json.loads(current[1][2])
    assert evidence["command_id"] == str(result.command_id)
    assert evidence["reason"] == "Owner changed their plan"
    duplicate = cancelling[1].execute(
        context(cancelling, key="cancel-once"), request(reservation_id)
    )
    assert duplicate.duplicate and duplicate.result == result.result
    assert counts(cancelling) == (1, 2, 2, 2)
    replacement = create(cancelling, key="replacement")
    assert replacement.result["reservation"]["id"] != reservation_id


def test_missing_and_someone_elses_reservation_are_indistinguishable(cancelling):
    reservation_id = create(cancelling).result["reservation"]["id"]
    errors = []
    for key, reference in (("foreign", reservation_id), ("missing", uuid4())):
        waiting = pending(cancelling, reference, key=key, user="bob")
        with pytest.raises(DomainError) as caught:
            confirm(cancelling, waiting, "bob")
        errors.append((caught.value.code, caught.value.message, caught.value.status))
    assert errors == [("NOT_FOUND", "Reservation is not available", 404)] * 2
    assert counts(cancelling) == (1, 1, 1, 1)


def test_offline_unknown_gpu_and_removed_create_grant_do_not_prevent_owner_cancellation(cancelling):
    service, _, sessions, ids, clock, _ = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    clock["now"] += timedelta(seconds=180)
    service.availability.rules = None
    with sessions.begin() as session:
        session.execute(delete(GpuDevice))
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["creator"]
            )
        )
    assert cancelled(cancelling, reservation_id).result["status"] == "cancelled"
    assert counts(cancelling) == (1, 2, 2, 2)


@pytest.mark.parametrize("version", [0, -1, True])
def test_cancellation_version_is_a_positive_strict_integer(cancelling, version):
    reservation_id = create(cancelling).result["reservation"]["id"]
    denied(lambda: pending(cancelling, reservation_id, version=version), "VALIDATION_ERROR")
    assert counts(cancelling) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "extra", [{"owner_id": str(uuid4())}, {"status": "cancelled"}, {"reason": "  "}]
)
def test_cancellation_rejects_actor_state_injection_and_empty_reason(cancelling, extra):
    reservation_id = create(cancelling).result["reservation"]["id"]
    denied(lambda: pending(cancelling, reservation_id, **extra), "VALIDATION_ERROR")


@pytest.mark.parametrize(
    "status", ["pending", "active", "completed", "cancelled", "rejected", "reclaiming"]
)
def test_only_confirmed_future_reservations_can_be_cancelled(cancelling, status):
    reservation_id = create(cancelling).result["reservation"]["id"]
    with cancelling[2].begin() as session:
        session.execute(update(Reservation).values(status=status))
    denied(lambda: cancelled(cancelling, reservation_id), "CONFLICT")
    assert counts(cancelling) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "delta,allowed",
    [(timedelta(microseconds=-1), True), (timedelta(0), False), (timedelta(seconds=1), False)],
)
def test_start_boundary_is_strict_and_independent_of_gpu_freshness(cancelling, delta, allowed):
    reservation_id = create(cancelling).result["reservation"]["id"]
    cancelling[4]["now"] = NOW + timedelta(hours=1) + delta
    if allowed:
        assert cancelled(cancelling, reservation_id).result["status"] == "cancelled"
    else:
        denied(lambda: cancelled(cancelling, reservation_id), "CONFLICT")
        assert counts(cancelling) == (1, 1, 1, 1)


def test_current_cancellation_permission_is_required_after_confirmation(cancelling):
    _, _, sessions, ids, _, _ = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    waiting = pending(cancelling, reservation_id)
    with sessions.begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
            )
        )
    denied(lambda: confirm(cancelling, waiting), "PERMISSION_DENIED")
    assert counts(cancelling) == (1, 1, 1, 1)


def test_missing_history_is_unknown_instead_of_inventing_a_version(cancelling):
    reservation_id = create(cancelling).result["reservation"]["id"]
    with cancelling[2].begin() as session:
        session.execute(delete(ReservationEvent))
    denied(lambda: cancelled(cancelling, reservation_id), "DEPENDENCY_UNAVAILABLE")
    assert counts(cancelling) == (1, 0, 1, 1)


def test_revoked_permission_after_update_rolls_back_cancellation_and_its_evidence(cancelling):
    _, _, sessions, ids, _, engine = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    waiting = pending(cancelling, reservation_id)
    fired = []

    def revoke(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            fired.append(True)
            connection.execute(
                delete(RoleBinding).where(
                    RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
                )
            )

    event.listen(engine, "after_cursor_execute", revoke)
    try:
        denied(lambda: confirm(cancelling, waiting), "PERMISSION_DENIED")
    finally:
        event.remove(engine, "after_cursor_execute", revoke)
    assert fired == [True] and counts(cancelling) == (1, 1, 1, 1)
    with sessions() as session:
        assert session.get(Reservation, reservation_id).status == "confirmed"


def test_node_only_grant_cannot_authorize_the_global_personal_cancellation_action(cancelling):
    _, _, sessions, ids, _, _ = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    with sessions.begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
            )
        )
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=ids["alice"],
                role_id=ids["reader"],
                scope_type="node",
                scope_id="node-a",
            )
        )
    denied(lambda: pending(cancelling, reservation_id), "PERMISSION_DENIED")


def test_old_version_and_cached_orm_row_cannot_overwrite_cancellation(cancelling):
    service, _, sessions, ids, _, _ = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    with sessions() as owner:
        cached = owner.get(Reservation, reservation_id)
        assert cached.status == "confirmed"
        cancelled(cancelling, reservation_id)
        execution = CommandExecution(
            actor_id=ids["alice"],
            command_id=uuid4(),
            request_id="stale-owner",
            scope=AccessScope(type="global", id="*"),
        )
        principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
        denied(
            lambda: service.cancel_in_transaction(
                owner,
                execution,
                principal,
                CancelReservation.model_validate(request(reservation_id).parameters),
            ),
            "CONFLICT",
        )
        owner.rollback()
    assert counts(cancelling) == (1, 2, 2, 2)


def test_owner_rollback_preserves_original_reservation_and_history(cancelling):
    service, _, sessions, ids, _, _ = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    original = history(cancelling)
    execution = CommandExecution(
        actor_id=ids["alice"],
        command_id=uuid4(),
        request_id="cancel-owner-abort",
        scope=AccessScope(type="global", id="*"),
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
    with pytest.raises(RuntimeError), sessions.begin() as session:
        service.cancel_in_transaction(
            session,
            execution,
            principal,
            CancelReservation.model_validate(request(reservation_id).parameters),
        )
        raise RuntimeError("fixture owner aborted")
    assert counts(cancelling) == (1, 1, 1, 1) and history(cancelling) == original
    with sessions() as session:
        assert session.get(Reservation, reservation_id).status == "confirmed"


def test_crossing_start_while_flushing_cancel_does_not_commit_a_cancellation(cancelling):
    _, _, sessions, _, clock, engine = cancelling
    reservation_id = create(cancelling).result["reservation"]["id"]
    waiting = pending(cancelling, reservation_id)
    fired = []

    def advance(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            fired.append(True)
            clock["now"] = NOW + timedelta(hours=1)

    event.listen(engine, "after_cursor_execute", advance)
    try:
        denied(lambda: confirm(cancelling, waiting), "CONFLICT")
    finally:
        event.remove(engine, "after_cursor_execute", advance)
    assert fired == [True] and counts(cancelling) == (1, 1, 1, 1)
    with sessions() as session:
        assert session.get(Reservation, reservation_id).status == "confirmed"


def test_postgres_two_cancellations_only_append_one_new_version(cancelling):
    if cancelling[5].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row and advisory locks")
    reservation_id = create(cancelling).result["reservation"]["id"]
    waiting = [pending(cancelling, reservation_id, key="race-" + str(index)) for index in range(2)]
    barrier = Barrier(2)

    def attempt(item):
        barrier.wait(timeout=5)
        try:
            return confirm(cancelling, item).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, waiting))
    assert sorted(outcomes) == ["CONFLICT", "succeeded"]
    assert counts(cancelling) == (1, 2, 2, 2)
