import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission, User
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import (
    ActorContext,
    CommandContext,
    CommandExecution,
    CommandRequest,
    ConfirmCommand,
)
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import AvailabilityWindow
from aris.modules.nova.commands import CreateReservationCommand
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import CreateReservation, MineQuery
from aris.modules.nova.reservation_service import ReservationService
from aris.modules.vega.models import GpuDevice, Node
from aris.modules.vega.target_service import VegaTargetService
from tests.test_availability import RULES, calendar, reservation
from tests.test_gpu_target_facts import NOW, node_values

KEY = SecretStr("fixture-only-reservation-confirmation-123456789")
NODE = AccessScope(type="node", id="node-a")


@pytest.fixture
def booking(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    clock = {"now": NOW}
    ids = {name: str(uuid4()) for name in ("alice", "bob", "create", "mine", "creator", "reader")}
    with sessions.begin() as session:
        session.add_all([Node(**node_values()), Node(**node_values("node-b"))])
        for name in ("alice", "bob"):
            session.add(User(id=ids[name], display_name=name, status="active", timezone="UTC"))
        for short in ("create", "mine"):
            session.add(Permission(id=ids[short], name="reservation." + short))
        for short in ("creator", "reader"):
            session.add(Role(id=ids[short], name="fixture-" + short))
        session.flush()
        session.add_all(
            [
                RolePermission(role_id=ids["creator"], permission_id=ids["create"]),
                RolePermission(role_id=ids["reader"], permission_id=ids["mine"]),
            ]
        )
        for name in ("alice", "bob"):
            for role, scope in (("creator", NODE), ("reader", AccessScope(type="global", id="*"))):
                session.add(
                    RoleBinding(
                        id=str(uuid4()),
                        user_id=ids[name],
                        role_id=ids[role],
                        scope_type=scope.type,
                        scope_id=scope.id,
                    )
                )
        session.add(
            GpuDevice(
                gpu_uuid="GPU-target",
                node_id="node-a",
                index=0,
                model="Fixture",
                memory_total_mib=49152,
                memory_used_mib=30000,
                utilization_percent=99,
                temperature_c=70,
                present=True,
            )
        )
    targets = VegaTargetService(sessions, settings, clock=lambda: clock["now"])
    availability = AvailabilityService(
        sessions, targets, rules=RULES, maintenance=(calendar(),), clock=lambda: clock["now"]
    )
    service = ReservationService(sessions, availability)
    commands = CommandService(
        sessions,
        [CreateReservationCommand(service)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    try:
        yield service, commands, sessions, ids, clock, engine
    finally:
        engine.dispose()


def context(state, user="alice", key="fixture-booking"):
    return CommandContext(actor_id=state[3][user], request_id="booking-" + key, idempotency_key=key)


def actor(state, user="alice", request_id="booking-confirm"):
    return ActorContext(actor_id=state[3][user], request_id=request_id)


def request(*, start=None, end=None, scope=NODE, **extra):
    start = start or NOW + timedelta(hours=1)
    return CommandRequest(
        command="reservation.create",
        scope=scope,
        parameters={
            "gpu_uuid": "GPU-target",
            "start_at": start.isoformat(),
            "end_at": (end or start + timedelta(hours=1)).isoformat(),
            "purpose": "Private fixture purpose",
            **extra,
        },
    )


def confirm(state, pending, user="alice"):
    return state[1].confirm(
        actor(state, user), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )


def create(state, user="alice", key="fixture-booking", **changes):
    return confirm(state, state[1].execute(context(state, user, key), request(**changes)), user)


def counts(state):
    with state[2]() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Reservation, ReservationEvent, DomainEvent, OutboxEntry)
        )


def denied(call, code):
    with pytest.raises(DomainError) as error:
        call()
    assert error.value.code == code


def test_default_confirmation_then_one_atomic_self_owned_booking_with_full_evidence(booking):
    service, commands, sessions, ids, _, _ = booking
    pending = commands.execute(context(booking), request())
    assert pending.state == "awaiting_confirmation" and counts(booking) == (0, 0, 0, 0)
    done = confirm(booking, pending)
    assert done.state == "succeeded" and counts(booking) == (1, 1, 1, 1)
    view = done.result["reservation"]
    assert view["status"] == "confirmed" and view["version"] == 1
    assert view["policy_version"] is None and done.result["assessment_scope"] == "basic_window"
    with sessions() as session:
        stored = session.get(Reservation, view["id"])
        history = session.scalar(select(ReservationEvent))
        published = session.scalar(select(DomainEvent))
        assert stored.owner_id == ids["alice"]
        assert history.payload["command_id"] == str(done.command_id)
        assert history.payload["request_id"] == "booking-confirm"
        assert history.payload["assessment"]["resource"]["node_id"] == "node-a"
        assert history.payload["assessment"]["rules_snapshot"] == RULES.model_dump()
        assert history.payload["maintenance_snapshot"] == calendar().model_dump(mode="json")
        assert published.causation_id == str(done.command_id)
        assert published.correlation_id == "booking-confirm"
        entry = session.scalar(select(AuditEntry).where(AuditEntry.action == "reservation.created"))
        assert "Private fixture purpose" not in json.dumps(entry.changes)
    replay = commands.execute(context(booking), request())
    assert replay.duplicate and replay.result == done.result and counts(booking) == (1, 1, 1, 1)
    assert len(service.mine(actor(booking), MineQuery()).reservations) == 1
    assert service.mine(actor(booking, "bob"), MineQuery()).reservations == ()


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_id": str(uuid4())},
        {"status": "active"},
        {"policy_version": "invented"},
        {"purpose": "  "},
        {"command_id": str(uuid4())},
    ],
)
def test_client_cannot_supply_owner_state_policy_or_execution_metadata(booking, extra):
    denied(lambda: booking[1].execute(context(booking), request(**extra)), "VALIDATION_ERROR")
    assert counts(booking) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "changed", ["stale", "missing", "scope", "revoke", "suspend", "rules", "maintenance", "expired"]
)
def test_confirmation_rechecks_current_dependencies_and_never_writes_on_failure(booking, changed):
    service, commands, sessions, ids, clock, _ = booking
    pending = commands.execute(context(booking), request())
    expected = {
        "stale": "STALE_DATA",
        "missing": "NOT_FOUND",
        "scope": "PERMISSION_DENIED",
        "revoke": "PERMISSION_DENIED",
        "suspend": "PERMISSION_DENIED",
        "rules": "NOT_EVALUATED",
        "maintenance": "NOT_EVALUATED",
    }
    if changed in {"stale", "expired"}:
        clock["now"] += timedelta(seconds=60 if changed == "stale" else 300)
    elif changed == "rules":
        service.availability.rules = None
    elif changed == "maintenance":
        service.availability = AvailabilityService(
            sessions, service.availability.targets, rules=RULES, clock=lambda: clock["now"]
        )
    else:
        with sessions.begin() as session:
            if changed == "missing":
                session.execute(update(GpuDevice).values(present=False))
            elif changed == "scope":
                session.execute(update(GpuDevice).values(node_id="node-b"))
            elif changed == "revoke":
                session.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["creator"]
                    )
                )
            elif changed == "suspend":
                session.execute(
                    update(User).where(User.id == ids["alice"]).values(status="suspended")
                )
    if changed == "expired":
        assert confirm(booking, pending).state == "expired"
    else:
        denied(lambda: confirm(booking, pending), expected[changed])
    assert counts(booking) == (0, 0, 0, 0)


def test_scope_is_derived_from_the_actual_target_and_key_does_not_hide_changed_content(booking):
    denied(
        lambda: booking[1].execute(
            context(booking), request(scope=AccessScope(type="node", id="node-b"))
        ),
        "PERMISSION_DENIED",
    )
    create(booking)
    denied(lambda: booking[1].execute(context(booking), request(purpose="different")), "CONFLICT")
    assert counts(booking) == (1, 1, 1, 1)


@pytest.mark.parametrize("mode", ["overlap", "maintenance", "duration", "notice", "coverage"])
def test_resource_and_basic_window_rejections_do_not_claim_success(booking, mode):
    service, _, sessions, ids, _, _ = booking
    changes = {}
    if mode == "overlap":
        with sessions.begin() as session:
            session.add(reservation(ids["bob"]))
    elif mode == "maintenance":
        service.availability = AvailabilityService(
            sessions,
            service.availability.targets,
            rules=RULES,
            maintenance=(
                calendar(
                    AvailabilityWindow(
                        start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2)
                    )
                ),
            ),
            clock=lambda: NOW,
        )
    elif mode == "duration":
        changes["end"] = NOW + timedelta(hours=6)
    elif mode == "notice":
        changes["start"] = NOW + timedelta(seconds=59)
    else:
        # The request must pass the horizon to reach the coverage check.
        changes["start"] = NOW + timedelta(days=7)
        service.availability = AvailabilityService(
            sessions,
            service.availability.targets,
            rules=RULES,
            maintenance=(calendar(end=NOW + timedelta(days=1)),),
            clock=lambda: NOW,
        )
    expected = (
        "CONFLICT"
        if mode == "overlap"
        else "NOT_EVALUATED"
        if mode == "coverage"
        else "POLICY_REJECTED"
    )
    denied(lambda: create(booking, **changes), expected)
    assert counts(booking) == ((1, 0, 0, 0) if mode == "overlap" else (0, 0, 0, 0))


def test_adjacent_half_open_windows_and_keyset_pages_are_private_and_stable(booking):
    service, _, _, _, _, _ = booking
    for index in range(4):
        create(booking, key="page-" + str(index), start=NOW + timedelta(hours=index + 1))
    create(booking, "bob", key="other-owner", start=NOW + timedelta(hours=6))
    seen = []
    cursor = None
    for _ in range(3):
        page = service.mine(actor(booking), MineQuery(limit=2, cursor=cursor))
        seen.extend(item.id for item in page.reservations)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4
    # Same created_at timestamps exercise the UUID tie-breaker, not offset paging.
    assert [str(item) for item in seen] == sorted([str(item) for item in seen], reverse=True)


def test_mine_rechecks_access_and_does_not_flush_owner_work(booking):
    service, _, sessions, ids, _, _ = booking
    create(booking)
    with sessions() as session:
        pending = User(id=str(uuid4()), display_name="Pending", status="active", timezone="UTC")
        session.add(pending)
        assert (
            len(service.mine_in_transaction(session, actor(booking), MineQuery()).reservations) == 1
        )
        assert pending in session.new
        session.rollback()
    with sessions.begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == ids["alice"], RoleBinding.role_id == ids["reader"]
            )
        )
    denied(lambda: service.mine(actor(booking), MineQuery()), "PERMISSION_DENIED")


def test_failed_or_aborted_owner_cannot_leave_reservation_or_success_evidence(booking):
    service, _, sessions, ids, _, _ = booking
    execution = CommandExecution(
        actor_id=ids["alice"], command_id=uuid4(), request_id="owner-abort", scope=NODE
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
    data = CreateReservation.model_validate(request().parameters)
    with pytest.raises(RuntimeError), sessions.begin() as session:
        service.create_in_transaction(session, execution, principal, data)
        raise RuntimeError("fixture owner abort")
    assert counts(booking) == (0, 0, 0, 0)
    with sessions.begin() as session:
        denied(
            lambda: service.create_in_transaction(
                session, execution, principal.model_copy(update={"user_id": uuid4()}), data
            ),
            "PERMISSION_DENIED",
        )
    assert counts(booking) == (0, 0, 0, 0)


@pytest.mark.parametrize("change", ["stale", "revoke", "scope"])
def test_post_insert_recheck_rolls_back_the_entire_batch_even_when_owner_catches(booking, change):
    service, _, sessions, ids, clock, engine = booking
    execution = CommandExecution(
        actor_id=ids["alice"], command_id=uuid4(), request_id="after-insert", scope=NODE
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Alice", timezone="UTC")
    fired = []

    def invalidate(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO RESERVATIONS"):
            fired.append(True)
            if change == "stale":
                clock["now"] += timedelta(seconds=60)
            elif change == "revoke":
                connection.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == ids["alice"],
                        RoleBinding.role_id == ids["creator"],
                    )
                )
            else:
                connection.execute(update(GpuDevice).values(node_id="node-b"))

    event.listen(engine, "after_cursor_execute", invalidate)
    try:
        with sessions.begin() as session:
            denied(
                lambda: service.create_in_transaction(
                    session,
                    execution,
                    principal,
                    CreateReservation.model_validate(request().parameters),
                ),
                "STALE_DATA" if change == "stale" else "PERMISSION_DENIED",
            )
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor="fixture",
                    action="fixture.owner",
                    target="owner",
                    request_id="owner-survived",
                    recorded_at=NOW,
                    changes={},
                )
            )
    finally:
        event.remove(engine, "after_cursor_execute", invalidate)
    assert fired == [True] and counts(booking) == (0, 0, 0, 0)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 1
        assert session.scalar(select(GpuDevice.node_id)) == "node-a"


def test_invalid_command_result_rolls_back_business_history_event_and_audit(booking):
    service, _, sessions, _, clock, _ = booking

    class InvalidResult(CreateReservationCommand):
        def execute(self, session, execution, principal, parameters):
            super().execute(session, execution, principal, parameters)
            return {"bad": float("nan")}

    commands = CommandService(
        sessions, [InvalidResult(service)], confirmation_key=KEY, clock=lambda: clock["now"]
    )
    pending = commands.execute(context(booking), request())
    denied(
        lambda: commands.confirm(
            actor(booking), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
        ),
        "INTERNAL_ERROR",
    )
    assert counts(booking) == (0, 0, 0, 0)
    with sessions() as session:
        assert session.get(CommandRecord, str(pending.command_id)).state == "awaiting_confirmation"
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 1


def test_postgres_mine_runs_in_an_actual_read_only_transaction(booking):
    service, _, _, _, _, engine = booking
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only transaction enforcement")
    done = create(booking)
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=connection) as session:
            result = service.mine_in_transaction(session, actor(booking), MineQuery())
            assert str(result.reservations[0].id) == done.result["reservation"]["id"]


def test_postgres_concurrent_confirmations_only_create_one_reservation(booking):
    if booking[5].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory and exclusion locks")
    pending = [booking[1].execute(context(booking, user), request()) for user in ("alice", "bob")]
    barrier = Barrier(2)

    def attempt(pair):
        user, item = pair
        barrier.wait(timeout=5)
        try:
            return confirm(booking, item, user).state
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, zip(("alice", "bob"), pending, strict=True)))
    assert sorted(outcomes) == ["CONFLICT", "succeeded"]
    assert counts(booking) == (1, 1, 1, 1)


@pytest.mark.parametrize("other_writer", ["commit", "rollback_after_notice_expires"])
def test_postgres_constraint_wait_rechecks_time_or_reports_real_conflict(booking, other_writer):
    service, commands, sessions, ids, clock, engine = booking
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL exclusion-constraint waiting")
    start = NOW + timedelta(seconds=90)
    pending = commands.execute(context(booking), request(start=start))
    inserted, waiter = Event(), {}
    owner = sessions()
    owner.begin()
    owner.add(reservation(ids["bob"], start=start))
    owner.flush()

    def record(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO RESERVATIONS"):
            waiter["pid"] = connection.scalar(text("SELECT pg_backend_pid()"))
            inserted.set()

    def attempt():
        try:
            return confirm(booking, pending).state
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
                    while True:
                        blocked = monitor.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE pid=:pid AND NOT granted)"
                            ),
                            {"pid": waiter["pid"]},
                        )
                        if blocked:
                            break
                        assert time.monotonic() < deadline, "writer never waited on exclusion"
                        time.sleep(0.02)
                if other_writer == "commit":
                    owner.commit()
                else:
                    clock["now"] += timedelta(seconds=35)
                    owner.rollback()
                assert future.result(timeout=5) == (
                    "CONFLICT" if other_writer == "commit" else "POLICY_REJECTED"
                )
            finally:
                owner.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", record)
        owner.close()
    assert counts(booking) == ((1, 0, 0, 0) if other_writer == "commit" else (0, 0, 0, 0))
    with sessions() as session:
        assert session.get(CommandRecord, str(pending.command_id)).state == "awaiting_confirmation"
