from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from aris.modules.nova.occupancy import ReservationOccupancyService
from aris.modules.nova.occupancy_schemas import CurrentReservationQuery
from aris.modules.nova.repo import ReservationRepository
from tests.test_availability import availability_state as availability_state
from tests.test_availability import reservation
from tests.test_gpu_target_facts import NOW


def projection(state, *targets):
    return ReservationOccupancyService(state[0], clock=lambda: NOW).current(
        CurrentReservationQuery(gpu_uuids=targets or ("GPU-target",))
    )


@pytest.mark.parametrize(
    "status", ["confirmed", "active", "reclaiming", "pending", "completed", "cancelled", "rejected"]
)
def test_only_current_occupying_states_appear_without_owner_or_purpose(availability_state, status):
    sessions, _, owner, _ = availability_state
    with sessions.begin() as session:
        session.add(reservation(owner, status=status, start=NOW))
    result = projection(availability_state)
    expected = status in {"confirmed", "active", "reclaiming"}
    assert result.gpus[0].state == ("occupied" if expected else "unoccupied")
    assert result.gpus[0].ends_at == (NOW + timedelta(hours=1) if expected else None)
    assert (
        owner not in result.model_dump_json() and "Private purpose" not in result.model_dump_json()
    )
    assert (
        result.assessment_scope == "current_reservations" and "available" not in result.model_dump()
    )


def test_half_open_edges_and_other_or_unknown_gpu_do_not_turn_into_hardware_claims(
    availability_state,
):
    sessions, _, owner, _ = availability_state
    with sessions.begin() as session:
        session.add_all(
            [
                reservation(owner, start=NOW - timedelta(hours=1), end=NOW),
                reservation(owner, start=NOW + timedelta(hours=1)),
                reservation(owner, gpu="GPU-other", start=NOW),
            ]
        )
    result = projection(availability_state, "GPU-other", "GPU-target", "GPU-unknown")
    assert [item.state for item in result.gpus] == ["occupied", "unoccupied", "unoccupied"]
    assert [item.gpu_uuid for item in result.gpus] == ["GPU-other", "GPU-target", "GPU-unknown"]
    assert result.as_of == NOW


def test_ambiguous_projection_is_unknown_instead_of_arbitrarily_selecting_a_row(
    availability_state, monkeypatch
):
    monkeypatch.setattr(
        ReservationRepository,
        "current_occupancy",
        lambda *args: {"GPU-target": (2, NOW + timedelta(hours=1))},
    )
    result = projection(availability_state)
    assert result.gpus[0].state == "unknown" and result.gpus[0].ends_at is None


def test_current_projection_is_one_aggregate_and_does_not_flush_caller_work(availability_state):
    sessions, _, owner, engine = availability_state
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with sessions() as session:
            pending = reservation(owner, start=NOW)
            session.add(pending)
            result = ReservationOccupancyService(
                sessions, clock=lambda: NOW
            ).current_in_transaction(
                session, CurrentReservationQuery(gpu_uuids=tuple(f"GPU-{i}" for i in range(100)))
            )
            assert pending in session.new and session.in_transaction()
            assert all(item.state == "unoccupied" for item in result.gpus)
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
            session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_current_projection_preserves_utc_instant_and_rejects_naive_clock(availability_state):
    service = ReservationOccupancyService(
        availability_state[0], clock=lambda: NOW.astimezone(timezone(timedelta(hours=9)))
    )
    request = CurrentReservationQuery(gpu_uuids=("GPU-target",))
    assert service.current(request).as_of == NOW
    service.clock = lambda: NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        service.current(request)
    with pytest.raises(ValidationError):
        CurrentReservationQuery(gpu_uuids=("GPU-target", "GPU-target"))
    with pytest.raises(ValidationError):
        CurrentReservationQuery(gpu_uuids=tuple(f"GPU-{i}" for i in range(101)))


def test_projection_works_under_database_read_only_transactions(availability_state):
    sessions, _, owner, engine = availability_state
    with sessions.begin() as session:
        session.add(reservation(owner, start=NOW))

    def read_only(connection):
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(engine, "begin", read_only)
    try:
        assert projection(availability_state).gpus[0].state == "occupied"
    finally:
        event.remove(engine, "begin", read_only)
