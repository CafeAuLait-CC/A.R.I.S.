import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, text, update

from aris.core.access.models import Permission, RolePermission
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandExecution, CommandRequest
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import AvailabilityWindow, MaintenanceCoverage
from aris.modules.nova.commands import CreateReservationCommand, ExtendReservationCommand
from aris.modules.nova.extension import ReservationExtensionService
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import ExtendReservation
from aris.modules.vega.models import GpuDevice, Node
from tests.test_availability import RULES, calendar, reservation
from tests.test_gpu_target_facts import NOW
from tests.test_reservation_cancellation import history
from tests.test_reservation_service import KEY, confirm, context, counts, create, denied
from tests.test_reservation_service import booking as booking


@pytest.fixture
def extending(booking):
    base, _, sessions, ids, clock, _ = booking
    for name in ("extend", "extend_own"):
        ids[name] = str(uuid4())
    with sessions.begin() as session:
        for name in ("extend", "extend_own"):
            session.add(Permission(id=ids[name], name="reservation." + name))
        session.flush()
        session.add_all(
            [
                RolePermission(role_id=ids["reader"], permission_id=ids["extend_own"]),
                RolePermission(role_id=ids["creator"], permission_id=ids["extend"]),
            ]
        )
    extension = ReservationExtensionService(sessions, base.availability, clock=lambda: clock["now"])
    commands = CommandService(
        sessions,
        [CreateReservationCommand(base), ExtendReservationCommand(extension)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    return extension, commands, *booking[2:]


def request(reservation_id, *, version=1, end=None, **extra):
    return CommandRequest(
        command="reservation.extend",
        scope=AccessScope(type="global", id="*"),
        parameters={
            "reservation_id": str(reservation_id),
            "expected_version": version,
            "new_end_at": (end or NOW + timedelta(hours=3)).isoformat(),
            "reason": "Owner needs more time",
            **extra,
        },
    )


def pending(state, reservation_id, key="extend-once", user="alice", **changes):
    return state[1].execute(context(state, user, key), request(reservation_id, **changes))


def extend(state, reservation_id, **changes):
    return confirm(state, pending(state, reservation_id, **changes))


def fresh_fixture(state, when):
    # Synthetic committed collector report, never an update to the running API.
    state[4]["now"] = when
    with state[2].begin() as session:
        session.execute(
            update(Node).values(
                observed_at=when,
                received_at=when,
                telemetry_at=when,
                telemetry_received_at=when,
                collector_health_at=when,
                telemetry_seq=Node.last_seq + 1,
                last_seq=Node.last_seq + 2,
            )
        )


def test_confirmed_extension_preserves_original_history_and_is_exactly_idempotent_in_db(extending):
    created = create(extending).result["reservation"]
    original = history(extending)
    waiting = pending(extending, created["id"])
    assert history(extending) == original
    result = confirm(extending, waiting)
    view = result.result["reservation"]
    assert view["start_at"] == created["start_at"] and view["version"] == 2
    assert view["end_at"] == (NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    assert view["policy_version"] is None and result.result["assessment_scope"] == "extension_delta"
    records = history(extending)
    assert records[:1] == original and records[1][0:2] == (2, "reservation.extended")
    payload = json.loads(records[1][2])
    assert payload["previous_end_at"] == (NOW + timedelta(hours=2)).isoformat()
    assert payload["assessment"]["query"]["start_at"] == (
        NOW + timedelta(hours=2)
    ).isoformat().replace("+00:00", "Z")
    assert payload["assessment"]["rules_snapshot"] == RULES.model_dump()
    assert payload["command_id"] == str(result.command_id)
    replay = extending[1].execute(context(extending, key="extend-once"), request(created["id"]))
    assert replay.duplicate and replay.result == result.result and counts(extending) == (1, 2, 2, 2)


def test_multiple_extensions_use_total_original_duration_not_only_each_increment(extending):
    reservation_id = create(extending).result["reservation"]["id"]
    extend(extending, reservation_id, end=NOW + timedelta(hours=4))
    extend(extending, reservation_id, key="second", version=2, end=NOW + timedelta(hours=5))
    denied(
        lambda: extend(
            extending,
            reservation_id,
            key="third",
            version=3,
            end=NOW + timedelta(hours=5, microseconds=1),
        ),
        "POLICY_REJECTED",
    )
    assert counts(extending) == (1, 3, 3, 3)
    with extending[2]() as session:
        assert utc(session.get(Reservation, reservation_id).end_at) == NOW + timedelta(hours=5)


def test_timezone_equivalent_end_has_the_same_confirmation_intent(extending):
    reservation_id = create(extending).result["reservation"]["id"]
    first = pending(extending, reservation_id)
    offset = (NOW + timedelta(hours=3)).astimezone(timezone(timedelta(hours=9)))
    again = pending(extending, reservation_id, end=offset)
    assert first.command_id == again.command_id and first.proof == again.proof


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_id": str(uuid4())},
        {"node_id": "node-b"},
        {"start_at": NOW.isoformat()},
        {"reason": " "},
        {"expected_version": True},
    ],
)
def test_extension_input_does_not_accept_identity_scope_start_or_invalid_version(extending, extra):
    reservation_id = create(extending).result["reservation"]["id"]
    denied(lambda: pending(extending, reservation_id, **extra), "VALIDATION_ERROR")
    assert counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize("end", [NOW + timedelta(hours=2), NOW + timedelta(hours=1, minutes=59)])
def test_extension_must_strictly_increase_end(extending, end):
    reservation_id = create(extending).result["reservation"]["id"]
    denied(lambda: extend(extending, reservation_id, end=end), "VALIDATION_ERROR")
    assert counts(extending) == (1, 1, 1, 1)


def test_foreign_and_missing_ids_are_indistinguishable(extending):
    reservation_id = create(extending).result["reservation"]["id"]
    errors = []
    for key, reference in (("foreign", reservation_id), ("missing", uuid4())):
        waiting = pending(extending, reference, key=key, user="bob")
        with pytest.raises(DomainError) as error:
            confirm(extending, waiting, "bob")
        errors.append((error.value.code, error.value.message, error.value.status))
    assert errors == [("NOT_FOUND", "Reservation is not available", 404)] * 2


@pytest.mark.parametrize("permission", ["extend", "extend_own"])
def test_both_global_personal_and_actual_node_permissions_are_required(extending, permission):
    reservation_id = create(extending).result["reservation"]["id"]
    waiting = pending(extending, reservation_id)
    with extending[2].begin() as session:
        session.execute(
            delete(RolePermission).where(RolePermission.permission_id == extending[3][permission])
        )
    denied(lambda: confirm(extending, waiting), "PERMISSION_DENIED")
    assert counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize(
    "changed", ["stale", "missing", "node", "rules", "maintenance", "policy", "origin", "history"]
)
def test_changed_dependencies_are_not_treated_as_approval(extending, changed):
    service, _, sessions, _, clock, _ = extending
    reservation_id = create(extending).result["reservation"]["id"]
    waiting = pending(extending, reservation_id)
    if changed == "stale":
        clock["now"] += timedelta(seconds=60)
    elif changed == "rules":
        service.availability.rules = None
    elif changed == "maintenance":
        service.availability = AvailabilityService(
            sessions, service.availability.targets, rules=RULES, clock=lambda: clock["now"]
        )
    else:
        with sessions.begin() as session:
            if changed == "missing":
                session.execute(delete(GpuDevice))
            elif changed == "node":
                session.execute(update(GpuDevice).values(node_id="node-b"))
            elif changed == "policy":
                session.execute(update(Reservation).values(policy_version="future-policy"))
            elif changed == "origin":
                session.execute(
                    update(ReservationEvent).values(payload={"assessment_scope": "unknown"})
                )
            elif changed == "history":
                session.execute(delete(ReservationEvent))
    code = {
        "stale": "STALE_DATA",
        "missing": "NOT_FOUND",
        "node": "PERMISSION_DENIED",
        "rules": "NOT_EVALUATED",
        "maintenance": "NOT_EVALUATED",
        "policy": "DEPENDENCY_UNAVAILABLE",
        "origin": "DEPENDENCY_UNAVAILABLE",
        "history": "DEPENDENCY_UNAVAILABLE",
    }[changed]
    denied(lambda: confirm(extending, waiting), code)
    assert counts(extending) == ((1, 0, 1, 1) if changed == "history" else (1, 1, 1, 1))


@pytest.mark.parametrize(
    "damage", ["marker_only", "actor", "window", "fingerprint", "maintenance", "gpu"]
)
def test_extension_requires_complete_coherent_creation_evidence_not_just_a_marker(
    extending, damage
):
    reservation_id = create(extending).result["reservation"]["id"]
    with extending[2].begin() as session:
        original = session.scalar(select(ReservationEvent))
        payload = json.loads(json.dumps(original.payload))
        if damage == "marker_only":
            payload = {"assessment_scope": "basic_window"}
        elif damage == "actor":
            original.actor = extending[3]["bob"]
        elif damage == "window":
            payload["reservation"]["end_at"] = (NOW + timedelta(hours=1, minutes=30)).isoformat()
        elif damage == "fingerprint":
            payload["maintenance_fingerprint"] = "0" * 64
        elif damage == "maintenance":
            known = calendar(
                AvailabilityWindow(
                    start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2)
                )
            )
            payload["maintenance_snapshot"] = known.model_dump(mode="json")
            payload["maintenance_fingerprint"] = MaintenanceCoverage.model_validate(
                payload["maintenance_snapshot"]
            ).fingerprint
        else:
            payload["assessment"]["resource"]["gpu_uuid"] = "GPU-other"
        original.payload = payload
    denied(lambda: extend(extending, reservation_id), "DEPENDENCY_UNAVAILABLE")
    assert counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize("status", ["pending", "completed", "cancelled", "rejected", "reclaiming"])
def test_extension_only_accepts_confirmed_or_active_states(extending, status):
    reservation_id = create(extending).result["reservation"]["id"]
    with extending[2].begin() as session:
        session.execute(update(Reservation).values(status=status))
    denied(lambda: extend(extending, reservation_id), "CONFLICT")


@pytest.mark.parametrize("seconds,allowed", [(61, True), (60, True), (59, False), (0, False)])
def test_active_extension_uses_notice_at_the_increment_start(extending, seconds, allowed):
    reservation_id = create(extending).result["reservation"]["id"]
    fresh_fixture(extending, NOW + timedelta(hours=2, seconds=-seconds))
    with extending[2].begin() as session:
        session.execute(update(Reservation).values(status="active"))
    if allowed:
        assert extend(extending, reservation_id).result["reservation"]["status"] == "active"
    else:
        denied(
            lambda: extend(extending, reservation_id),
            "CONFLICT" if seconds == 0 else "POLICY_REJECTED",
        )
        assert counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize("mode", ["reservation", "maintenance", "coverage"])
def test_increment_must_have_no_conflict_and_complete_maintenance_coverage(extending, mode):
    service, _, sessions, ids, _, _ = extending
    reservation_id = create(extending).result["reservation"]["id"]
    delta = AvailabilityWindow(start_at=NOW + timedelta(hours=2), end_at=NOW + timedelta(hours=3))
    if mode == "reservation":
        with sessions.begin() as session:
            session.add(reservation(ids["bob"], start=delta.start_at, end=delta.end_at))
    else:
        known = (
            calendar(delta)
            if mode == "maintenance"
            else calendar(end=NOW + timedelta(hours=2, minutes=30))
        )
        service.availability = AvailabilityService(
            sessions,
            service.availability.targets,
            rules=RULES,
            maintenance=(known,),
            clock=lambda: NOW,
        )
    denied(
        lambda: extend(extending, reservation_id),
        {"reservation": "CONFLICT", "maintenance": "POLICY_REJECTED", "coverage": "NOT_EVALUATED"}[
            mode
        ],
    )


@pytest.mark.parametrize("changed_read", [2, 3, 4])
def test_actual_node_cannot_change_between_permission_and_assessment_reads(extending, changed_read):
    service = extending[0]
    reservation_id = create(extending).result["reservation"]["id"]
    original = service.availability.targets
    calls = []

    class MovingTarget:
        def gpu_in_transaction(self, session, query):
            fact = original.gpu_in_transaction(session, query)
            calls.append(True)
            return (
                fact.model_copy(update={"node_id": "node-b"})
                if len(calls) == changed_read
                else fact
            )

    service.availability.targets = MovingTarget()
    denied(lambda: extend(extending, reservation_id), "PERMISSION_DENIED")
    assert counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize("change", ["time", "permission"])
def test_post_update_expiry_or_revocation_rolls_back_end_and_evidence(extending, change):
    _, _, sessions, ids, clock, engine = extending
    reservation_id = create(extending).result["reservation"]["id"]
    waiting = pending(extending, reservation_id)
    fired = []

    def invalidate(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            fired.append(True)
            if change == "time":
                clock["now"] = NOW + timedelta(hours=2)
            else:
                connection.execute(
                    delete(RolePermission).where(RolePermission.permission_id == ids["extend"])
                )

    event.listen(engine, "after_cursor_execute", invalidate)
    try:
        denied(
            lambda: confirm(extending, waiting),
            "CONFLICT" if change == "time" else "PERMISSION_DENIED",
        )
    finally:
        event.remove(engine, "after_cursor_execute", invalidate)
    assert fired == [True] and counts(extending) == (1, 1, 1, 1)


def test_owner_rollback_does_not_commit_an_extended_window_or_event(extending):
    service, _, sessions, ids, _, _ = extending
    reservation_id = create(extending).result["reservation"]["id"]
    original = history(extending)
    execution = CommandExecution(
        actor_id=ids["alice"],
        command_id=uuid4(),
        request_id="extend-abort",
        scope=AccessScope(type="global", id="*"),
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
    with pytest.raises(RuntimeError), sessions.begin() as session:
        service.extend_in_transaction(
            session,
            execution,
            principal,
            ExtendReservation.model_validate(request(reservation_id).parameters),
        )
        raise RuntimeError("fixture owner aborted")
    assert history(extending) == original and counts(extending) == (1, 1, 1, 1)
    with sessions() as session:
        assert utc(session.get(Reservation, reservation_id).end_at) == NOW + timedelta(hours=2)


def test_end_crossed_after_final_fact_read_still_cannot_commit(extending):
    service, _, sessions, _, clock, _ = extending
    reservation_id = create(extending).result["reservation"]["id"]
    fresh_fixture(extending, NOW + timedelta(hours=2, microseconds=-1))
    original = service.availability.targets
    calls = []

    class CrossingTarget:
        def gpu_in_transaction(self, session, query):
            fact = original.gpu_in_transaction(session, query)
            calls.append(True)
            if len(calls) == 4:
                clock["now"] = NOW + timedelta(hours=2)
            return fact

    service.availability = AvailabilityService(
        sessions,
        CrossingTarget(),
        rules=RULES.model_copy(update={"min_notice_seconds": 0}),
        maintenance=(calendar(),),
        clock=lambda: clock["now"],
    )
    denied(lambda: extend(extending, reservation_id), "CONFLICT")
    assert len(calls) == 4 and counts(extending) == (1, 1, 1, 1)


@pytest.mark.parametrize("other_writer", ["commit", "rollback_after_end"])
def test_postgres_update_exclusion_wait_never_commits_an_invalid_extension(extending, other_writer):
    _, _, sessions, ids, clock, engine = extending
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL UPDATE exclusion-constraint waiting")
    reservation_id = create(extending).result["reservation"]["id"]
    waiting = pending(extending, reservation_id)
    owner = sessions()
    owner.begin()
    owner.add(reservation(ids["bob"], start=NOW + timedelta(hours=2)))
    owner.flush()
    inserted, waiter = Event(), {}

    def record(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE RESERVATIONS"):
            waiter["pid"] = connection.scalar(text("SELECT pg_backend_pid()"))
            inserted.set()

    def attempt():
        try:
            return confirm(extending, waiting).state
        except DomainError as error:
            return error.code

    event.listen(engine, "before_cursor_execute", record)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(attempt)
            try:
                assert inserted.wait(5)
                deadline = time.monotonic() + 5
                with engine.connect() as monitor:
                    while not monitor.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                        ),
                        {"pid": waiter["pid"]},
                    ):
                        assert time.monotonic() < deadline, "UPDATE never waited on exclusion"
                        time.sleep(0.02)
                if other_writer == "commit":
                    owner.commit()
                else:
                    clock["now"] = NOW + timedelta(hours=2)
                    owner.rollback()
                assert future.result(timeout=5) == "CONFLICT"
            finally:
                owner.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", record)
        owner.close()
    assert counts(extending) == ((2, 1, 1, 1) if other_writer == "commit" else (1, 1, 1, 1))
    with sessions() as session:
        assert utc(session.get(Reservation, reservation_id).end_at) == NOW + timedelta(hours=2)


@pytest.mark.parametrize("race", ["extend", "adjacent_create"])
def test_postgres_only_one_competing_window_change_can_commit(extending, race):
    if extending[5].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory, row and exclusion locks")
    reservation_id = create(extending).result["reservation"]["id"]
    first = pending(extending, reservation_id, key="race-first")
    if race == "extend":
        second = pending(
            extending, reservation_id, key="race-second", end=NOW + timedelta(hours=2, minutes=30)
        )
        other_user = "alice"
    else:
        from tests.test_reservation_service import request as create_request

        second = extending[1].execute(
            context(extending, "bob", "race-second"), create_request(start=NOW + timedelta(hours=2))
        )
        other_user = "bob"
    barrier = Barrier(2)

    def attempt(pair):
        user, waiting = pair
        barrier.wait(timeout=5)
        try:
            return confirm(extending, waiting, user).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (("alice", first), (other_user, second))))
    assert sorted(outcomes) == ["CONFLICT", "succeeded"]
    assert counts(extending)[1:] == (2, 2, 2)
