from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, update

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission, User
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import CommandExecution, CommandRequest
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.nova.administration import ReservationAdministrationService
from aris.modules.nova.commands import (
    CancelAnyReservationCommand,
    CreateReservationCommand,
    ReleaseAnyReservationCommand,
)
from aris.modules.nova.models import Reservation
from aris.modules.nova.reservation_schemas import AdministrativeReservationChange
from aris.modules.vega.models import GpuDevice
from tests.test_gpu_target_facts import NOW
from tests.test_reservation_cancellation import history
from tests.test_reservation_service import KEY, confirm, context, counts, create, denied
from tests.test_reservation_service import booking as booking


@pytest.fixture
def administration(booking):
    base, _, sessions, ids, clock, _ = booking
    ids["manager"] = str(uuid4())
    with sessions.begin() as session:
        session.add(Role(id=ids["manager"], name="fixture_reservation_manager"))
        for operation in ("cancel", "release"):
            key = operation + "_any"
            ids[key] = str(uuid4())
            session.add(Permission(id=ids[key], name="reservation." + key))
        session.flush()
        for operation in ("cancel", "release"):
            session.add(
                RolePermission(role_id=ids["manager"], permission_id=ids[operation + "_any"])
            )
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=ids["bob"],
                role_id=ids["manager"],
                scope_type="global",
                scope_id="*",
            )
        )
    admin = ReservationAdministrationService(sessions, clock=lambda: clock["now"])
    commands = CommandService(
        sessions,
        [
            CreateReservationCommand(base),
            CancelAnyReservationCommand(admin),
            ReleaseAnyReservationCommand(admin),
        ],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    return admin, commands, *booking[2:]


def request(state, reservation_id, operation="cancel", **extra):
    return CommandRequest(
        command="reservation." + operation + "_any",
        scope=AccessScope(type="global", id="*"),
        parameters={
            "reservation_id": reservation_id,
            "expected_owner_id": state[3]["alice"],
            "expected_version": 1,
            "reason": "Fixture administrator ends the allocation",
            **extra,
        },
    )


def pending(state, reservation_id, operation="cancel", key="admin-once", user="bob", **extra):
    return state[1].execute(
        context(state, user, key), request(state, reservation_id, operation, **extra)
    )


@pytest.mark.parametrize("operation", ["cancel", "release"])
def test_default_confirmed_administrative_reduction_has_distinct_actor_owner_and_no_duplicate(
    administration, operation
):
    state = administration
    created = create(state).result["reservation"]
    original = history(state)
    if operation == "release":
        state[4]["now"] = NOW + timedelta(hours=3)
    waiting = pending(state, created["id"], operation)
    assert counts(state) == (1, 1, 1, 1)
    result = confirm(state, waiting, "bob")
    assert result.result["administrative"] is True
    assert (
        result.result["owner_id"] == state[3]["alice"]
        and result.result["actor_id"] == state[3]["bob"]
    )
    view = result.result["reservation"]
    assert "purpose" not in view and "policy_version" not in view
    assert (
        view["status"] == ("cancelled" if operation == "cancel" else "completed")
        and view["version"] == 2
    )
    assert (view["start_at"], view["end_at"]) == (created["start_at"], created["end_at"])
    assert history(state)[:1] == original
    replay = state[1].execute(
        context(state, "bob", "admin-once"), request(state, created["id"], operation)
    )
    assert replay.duplicate and replay.result == result.result and counts(state) == (1, 2, 2, 2)
    with state[2]() as session:
        created_fact = session.scalar(
            select(DomainEvent).where(DomainEvent.event_type == "reservation.created")
        )
        assert created_fact.payload["owner_id"] == state[3]["alice"]
        assert created_fact.payload["administrative"] is False
        fact = session.scalar(
            select(DomainEvent).where(
                DomainEvent.event_type
                == ("reservation.cancelled" if operation == "cancel" else "reservation.released")
            )
        )
        assert fact.actor == state[3]["bob"] and fact.payload["owner_id"] == state[3]["alice"]
        assert fact.payload["administrative"] is True
        audit = session.scalar(select(AuditEntry).where(AuditEntry.action == fact.event_type))
        assert (
            audit.changes["owner_id"] == state[3]["alice"]
            and audit.changes["reason"] == "Fixture administrator ends the allocation"
        )
        assert set(session.scalars(select(GpuDevice.utilization_percent))) == {99}


@pytest.mark.parametrize("grant", ["missing", "node_only", "identity_admin"])
def test_personal_node_or_identity_role_does_not_imply_global_administrative_permission(
    administration, grant
):
    state = administration
    reservation_id = create(state).result["reservation"]["id"]
    with state[2].begin() as session:
        binding = session.scalar(
            select(RoleBinding).where(
                RoleBinding.user_id == state[3]["bob"], RoleBinding.role_id == state[3]["manager"]
            )
        )
        if grant == "node_only":
            binding.scope_type, binding.scope_id = "node", "node-a"
        else:
            session.delete(binding)
            if grant == "identity_admin":
                role = Role(id=str(uuid4()), name="identity_admin")
                session.add(role)
                session.flush()
                session.add(
                    RoleBinding(
                        id=str(uuid4()),
                        user_id=state[3]["bob"],
                        role_id=role.id,
                        scope_type="global",
                        scope_id="*",
                    )
                )
    denied(lambda: pending(state, reservation_id), "PERMISSION_DENIED")
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "damage",
    [
        {"actor_id": str(uuid4())},
        {"owner_id": str(uuid4())},
        {"reason": " "},
        {"expected_version": True},
    ],
)
def test_input_cannot_select_actor_or_replace_owner_or_omit_a_reason(administration, damage):
    reservation_id = create(administration).result["reservation"]["id"]
    denied(lambda: pending(administration, reservation_id, **damage), "VALIDATION_ERROR")


@pytest.mark.parametrize("change", ["owner", "version", "missing"])
def test_preconditions_bind_actual_persistent_owner_and_version(administration, change):
    state = administration
    reservation_id = create(state).result["reservation"]["id"]
    waiting = pending(
        state,
        reservation_id,
        expected_owner_id=state[3]["bob"] if change == "owner" else state[3]["alice"],
        expected_version=2 if change == "version" else 1,
    )
    if change == "missing":
        waiting = pending(state, str(uuid4()), key="admin-missing")
    denied(
        lambda: confirm(state, waiting, "bob"), "NOT_FOUND" if change == "missing" else "CONFLICT"
    )
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "operation,status",
    [
        ("cancel", "active"),
        ("cancel", "pending"),
        ("cancel", "cancelled"),
        ("release", "reclaiming"),
        ("release", "completed"),
        ("release", "rejected"),
    ],
)
def test_invalid_states_do_not_gain_an_administrative_bypass(administration, operation, status):
    state = administration
    reservation_id = create(state).result["reservation"]["id"]
    if operation == "release":
        state[4]["now"] = NOW + timedelta(hours=1)
    with state[2].begin() as session:
        session.execute(update(Reservation).values(status=status))
    denied(lambda: confirm(state, pending(state, reservation_id, operation), "bob"), "CONFLICT")
    assert counts(state) == (1, 1, 1, 1)


@pytest.mark.parametrize("operation", ["cancel", "release"])
def test_unknown_gpu_and_policy_do_not_prevent_authorized_capacity_reduction(
    administration, operation
):
    state = administration
    reservation_id = create(state).result["reservation"]["id"]
    if operation == "release":
        state[4]["now"] = NOW + timedelta(hours=1)
    with state[2].begin() as session:
        session.execute(delete(GpuDevice))
        session.execute(update(Reservation).values(policy_version="unavailable-future-policy"))
        session.execute(update(User).where(User.id == state[3]["alice"]).values(status="suspended"))
    result = confirm(state, pending(state, reservation_id, operation), "bob")
    assert "policy_version" not in result.result["reservation"]
    with state[2]() as session:
        assert (
            session.get(Reservation, reservation_id).policy_version == "unavailable-future-policy"
        )
    assert counts(state) == (1, 2, 2, 2)


@pytest.mark.parametrize("operation", ["cancel", "release"])
@pytest.mark.parametrize("change", ["permission", "time"])
def test_flush_rechecks_roll_back_administrative_change_and_preserve_history(
    administration, operation, change
):
    state = administration
    clock = state[4]
    engine = state[5]
    reservation_id = create(state).result["reservation"]["id"]
    if operation == "release":
        clock["now"] = NOW + timedelta(hours=1)
    waiting = pending(state, reservation_id, operation)
    before = history(state)
    fired = []

    def invalidate(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            fired.append(True)
            if change == "permission":
                connection.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == state[3]["bob"],
                        RoleBinding.role_id == state[3]["manager"],
                    )
                )
            else:
                clock["now"] = NOW + timedelta(hours=1, seconds=0 if operation == "cancel" else -1)

    event.listen(engine, "after_cursor_execute", invalidate)
    try:
        denied(
            lambda: confirm(state, waiting, "bob"),
            "PERMISSION_DENIED" if change == "permission" else "CONFLICT",
        )
    finally:
        event.remove(engine, "after_cursor_execute", invalidate)
    assert fired == [True] and counts(state) == (1, 1, 1, 1) and history(state) == before
    with state[2]() as session:
        assert session.get(CommandRecord, str(waiting.command_id)).state == "awaiting_confirmation"


def test_owner_abort_and_false_principal_cannot_commit_administrative_effects(administration):
    state = administration
    service = state[0]
    reservation_id = create(state).result["reservation"]["id"]
    before = history(state)
    execution = CommandExecution(
        actor_id=state[3]["bob"],
        command_id=uuid4(),
        request_id="admin-owner-abort",
        scope=AccessScope(type="global", id="*"),
    )
    principal = UserPrincipal(user_id=state[3]["bob"], display_name="Operator", timezone="UTC")
    data = AdministrativeReservationChange.model_validate(request(state, reservation_id).parameters)
    with pytest.raises(RuntimeError), state[2].begin() as session:
        service.cancel_in_transaction(session, execution, principal, data)
        raise RuntimeError("fixture owner abort")
    with state[2].begin() as session:
        denied(
            lambda: service.cancel_in_transaction(
                session, execution, principal.model_copy(update={"user_id": uuid4()}), data
            ),
            "PERMISSION_DENIED",
        )
    assert counts(state) == (1, 1, 1, 1) and history(state) == before


@pytest.mark.parametrize("operation", ["cancel", "release"])
def test_postgres_two_administrators_actions_only_append_one_version(administration, operation):
    state = administration
    if state[5].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory and row locks")
    reservation_id = create(state).result["reservation"]["id"]
    state[3]["charlie"] = str(uuid4())
    with state[2].begin() as session:
        session.add(
            User(
                id=state[3]["charlie"],
                display_name="Second fixture operator",
                timezone="UTC",
                status="active",
            )
        )
        session.flush()
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=state[3]["charlie"],
                role_id=state[3]["manager"],
                scope_type="global",
                scope_id="*",
            )
        )
    if operation == "release":
        state[4]["now"] = NOW + timedelta(hours=1)
    waiting = [
        (user, pending(state, reservation_id, operation, key="race-" + user, user=user))
        for user in ("bob", "charlie")
    ]
    barrier = Barrier(2)

    def attempt(pair):
        user, item = pair
        barrier.wait(timeout=5)
        try:
            return confirm(state, item, user).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, waiting))
    assert sorted(results) == ["CONFLICT", "succeeded"] and counts(state) == (1, 2, 2, 2)
