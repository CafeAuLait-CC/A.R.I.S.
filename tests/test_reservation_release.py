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
from aris.modules.nova.commands import CreateReservationCommand, ReleaseReservationCommand
from aris.modules.nova.models import Reservation
from aris.modules.nova.reservation_schemas import ReleaseReservation
from aris.modules.vega.models import GpuDevice
from tests.test_gpu_target_facts import NOW
from tests.test_reservation_cancellation import history
from tests.test_reservation_service import KEY, confirm, context, counts, create, denied
from tests.test_reservation_service import booking as booking


@pytest.fixture
def releasing(booking):
    service, _, sessions, ids, clock, _ = booking
    permission = str(uuid4())
    with sessions.begin() as session:
        session.add(Permission(id=permission, name="reservation.release_own"))
        session.flush()
        session.add(RolePermission(role_id=ids["reader"], permission_id=permission))
    commands = CommandService(
        sessions,
        [CreateReservationCommand(service), ReleaseReservationCommand(service)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    return service, commands, *booking[2:]


def request(reservation_id, version=1, **extra):
    return CommandRequest(
        command="reservation.release",
        scope=AccessScope(type="global", id="*"),
        parameters={
            "reservation_id": str(reservation_id),
            "expected_version": version,
            "reason": "Owner finished using the allocation",
            **extra,
        },
    )


def pending(state, reservation_id, key="release-once", user="alice", **changes):
    return state[1].execute(context(state, user, key), request(reservation_id, **changes))


def release(state, reservation_id, **changes):
    return confirm(state, pending(state, reservation_id, **changes))


@pytest.mark.parametrize(
    "elapsed",
    [timedelta(hours=1), timedelta(hours=1, minutes=20), timedelta(hours=2), timedelta(hours=3)],
)
def test_release_preserves_schedule_and_records_bounded_effective_end(releasing, elapsed):
    created = create(releasing).result["reservation"]
    original = history(releasing)
    releasing[4]["now"] = NOW + elapsed
    waiting = pending(releasing, created["id"])
    assert counts(releasing) == (1, 1, 1, 1)
    result = confirm(releasing, waiting)
    changed = result.result["reservation"]
    assert changed["status"] == "completed" and changed["version"] == 2
    assert changed["start_at"] == created["start_at"] and changed["end_at"] == created["end_at"]
    expected = min(NOW + elapsed, NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    assert result.result["effective_end_at"] == expected
    entries = history(releasing)
    assert entries[:1] == original and entries[1][:2] == (2, "reservation.released")
    evidence = json.loads(entries[1][2])
    assert evidence["effective_end_at"] == min(NOW + elapsed, NOW + timedelta(hours=2)).isoformat()
    assert evidence["command_id"] == str(result.command_id)
    again = releasing[1].execute(context(releasing, key="release-once"), request(created["id"]))
    assert again.duplicate and again.result == result.result and counts(releasing) == (1, 2, 2, 2)
    with releasing[2]() as session:
        gpu = session.scalar(select(GpuDevice))
        assert gpu.utilization_percent == 99 and gpu.memory_used_mib == 30000


def test_release_can_complete_active_fixture_without_gpu_or_create_permission(releasing):
    _, _, sessions, ids, clock, _ = releasing
    reservation_id = create(releasing).result["reservation"]["id"]
    clock["now"] = NOW + timedelta(hours=1, minutes=1)
    # Automatic activation is not implemented; set an explicit storage fixture.
    with sessions.begin() as session:
        session.execute(update(Reservation).values(status="active"))
        session.execute(delete(GpuDevice))
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["creator"]
            )
        )
    assert release(releasing, reservation_id).result["reservation"]["status"] == "completed"


def test_release_before_start_requires_cancel_instead(releasing):
    reservation_id = create(releasing).result["reservation"]["id"]
    releasing[4]["now"] = NOW + timedelta(hours=1, microseconds=-1)
    denied(lambda: release(releasing, reservation_id), "CONFLICT")
    assert counts(releasing) == (1, 1, 1, 1)


@pytest.mark.parametrize("status", ["pending", "completed", "cancelled", "rejected", "reclaiming"])
def test_release_does_not_invent_transitions_from_other_states(releasing, status):
    reservation_id = create(releasing).result["reservation"]["id"]
    releasing[4]["now"] = NOW + timedelta(hours=1)
    with releasing[2].begin() as session:
        session.execute(update(Reservation).values(status=status))
    denied(lambda: release(releasing, reservation_id), "CONFLICT")
    assert counts(releasing) == (1, 1, 1, 1)


def test_release_does_not_distinguish_foreign_or_missing_reservations(releasing):
    reservation_id = create(releasing).result["reservation"]["id"]
    releasing[4]["now"] = NOW + timedelta(hours=1)
    caught = []
    for key, target in (("foreign", reservation_id), ("missing", uuid4())):
        waiting = pending(releasing, target, key=key, user="bob")
        with pytest.raises(DomainError) as error:
            confirm(releasing, waiting, "bob")
        caught.append((error.value.code, error.value.message, error.value.status))
    assert caught == [("NOT_FOUND", "Reservation is not available", 404)] * 2


def test_current_global_release_permission_is_not_inferred_from_create(releasing):
    _, _, sessions, ids, clock, _ = releasing
    reservation_id = create(releasing).result["reservation"]["id"]
    clock["now"] = NOW + timedelta(hours=1)
    waiting = pending(releasing, reservation_id)
    with sessions.begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
            )
        )
    denied(lambda: confirm(releasing, waiting), "PERMISSION_DENIED")
    assert counts(releasing) == (1, 1, 1, 1)


@pytest.mark.parametrize("change", ["clock", "permission"])
def test_post_update_clock_or_authority_change_rolls_back_release(releasing, change):
    _, _, sessions, ids, clock, engine = releasing
    reservation_id = create(releasing).result["reservation"]["id"]
    clock["now"] = NOW + timedelta(hours=1)
    waiting = pending(releasing, reservation_id)
    fired = []

    def invalidate(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            fired.append(True)
            if change == "clock":
                clock["now"] = NOW + timedelta(hours=1, microseconds=-1)
            else:
                connection.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
                    )
                )

    event.listen(engine, "after_cursor_execute", invalidate)
    try:
        denied(
            lambda: confirm(releasing, waiting),
            "CONFLICT" if change == "clock" else "PERMISSION_DENIED",
        )
    finally:
        event.remove(engine, "after_cursor_execute", invalidate)
    assert fired == [True] and counts(releasing) == (1, 1, 1, 1)
    with sessions() as session:
        assert session.get(Reservation, reservation_id).status == "confirmed"


def test_owner_rollback_preserves_original_schedule_state_and_history(releasing):
    service, _, sessions, ids, clock, _ = releasing
    reservation_id = create(releasing).result["reservation"]["id"]
    original = history(releasing)
    clock["now"] = NOW + timedelta(hours=1)
    execution = CommandExecution(
        actor_id=ids["alice"],
        command_id=uuid4(),
        request_id="release-abort",
        scope=AccessScope(type="global", id="*"),
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
    with pytest.raises(RuntimeError), sessions.begin() as session:
        service.release_in_transaction(
            session,
            execution,
            principal,
            ReleaseReservation.model_validate(request(reservation_id).parameters),
        )
        raise RuntimeError("fixture owner aborted")
    assert counts(releasing) == (1, 1, 1, 1) and history(releasing) == original


def test_postgres_concurrent_release_only_appends_one_completion(releasing):
    if releasing[5].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory and row locks")
    reservation_id = create(releasing).result["reservation"]["id"]
    releasing[4]["now"] = NOW + timedelta(hours=1)
    waiting = [
        pending(releasing, reservation_id, key="release-race-" + str(index)) for index in range(2)
    ]
    barrier = Barrier(2)

    def attempt(item):
        barrier.wait(timeout=5)
        try:
            return confirm(releasing, item).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, waiting))
    assert sorted(outcomes) == ["CONFLICT", "succeeded"]
    assert counts(releasing) == (1, 2, 2, 2)
