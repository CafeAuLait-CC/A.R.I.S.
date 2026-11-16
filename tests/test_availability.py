import json
from datetime import UTC, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text, update

from aris.core.access.models import User
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import (
    AvailabilityQuery,
    AvailabilityWindow,
    BasicReservationRules,
    MaintenanceCoverage,
)
from aris.modules.nova.models import Reservation
from aris.modules.vega.models import GpuDevice, Node
from aris.modules.vega.target_service import VegaTargetService
from tests.test_gpu_target_facts import NOW, node_values

RULES = BasicReservationRules(
    max_duration_seconds=4 * 3600, min_notice_seconds=60, max_advance_seconds=7 * 86400
)


def query(start=None, end=None, gpu="GPU-target"):
    start = start or NOW + timedelta(hours=1)
    return AvailabilityQuery(gpu_uuid=gpu, start_at=start, end_at=end or start + timedelta(hours=1))


def calendar(*windows, gpu="GPU-target", start=NOW, end=None):
    return MaintenanceCoverage(
        gpu_uuid=gpu,
        coverage=AvailabilityWindow(start_at=start, end_at=end or NOW + timedelta(days=8)),
        windows=windows,
    )


@pytest.fixture
def availability_state(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    owner = str(uuid4())
    with sessions.begin() as session:
        session.add(Node(**node_values()))
        session.add(User(id=owner, display_name="Private owner", status="active", timezone="UTC"))
        session.flush()
        session.add(
            GpuDevice(
                gpu_uuid="GPU-target",
                node_id="node-a",
                index=0,
                model="Fixture GPU",
                memory_total_mib=49152,
                memory_used_mib=30000,
                utilization_percent=99,
                temperature_c=70,
                present=True,
            )
        )
    targets = VegaTargetService(sessions, settings, clock=lambda: NOW)
    try:
        yield sessions, targets, owner, engine
    finally:
        engine.dispose()


def service(state, **changes):
    sessions, targets, _, _ = state
    arguments = {"rules": RULES, "maintenance": (calendar(),), "clock": lambda: NOW}
    arguments.update(changes)
    return AvailabilityService(sessions, targets, **arguments)


def reservation(owner, *, start=None, end=None, gpu="GPU-target", status="confirmed"):
    start = start or NOW + timedelta(hours=1)
    return Reservation(
        id=str(uuid4()),
        owner_id=owner,
        gpu_uuid=gpu,
        start_at=start,
        end_at=end or start + timedelta(hours=1),
        status=status,
        purpose="Private purpose",
        policy_version=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_explicit_known_rules_and_empty_covered_calendar_allow_future_busy_gpu(availability_state):
    result = service(availability_state).assess(query())
    assert result.code == "AVAILABLE" and result.available is True
    assert result.resource.node_id == "node-a"
    assert result.rules_snapshot == RULES and result.rules_fingerprint == RULES.fingerprint
    assert result.reservation_conflicts.total == result.maintenance_conflicts.total == 0
    # Fixture GPU is currently 99% busy; that is separate from its future window.
    assert "permission" not in result.model_dump() and "quota" not in result.model_dump()


def test_defaults_are_unknown_instead_of_inventing_rules_or_zero_conflicts(availability_state):
    sessions, targets, _, _ = availability_state
    result = AvailabilityService(sessions, targets, clock=lambda: NOW).assess(query())
    assert result.code == "NOT_EVALUATED" and result.available is None
    assert result.reason == "RULES_UNCONFIGURED"
    assert result.rules_snapshot is result.rules_fingerprint is None
    assert result.reservation_conflicts is result.maintenance_conflicts is None


@pytest.mark.parametrize("coverage", [(), (calendar(gpu="another-GPU"),)])
def test_missing_or_other_target_maintenance_is_not_an_empty_schedule(availability_state, coverage):
    result = service(availability_state, maintenance=coverage).assess(query())
    assert result.code == "NOT_EVALUATED" and result.available is None
    assert result.reason == "MAINTENANCE_UNCONFIGURED"
    assert result.maintenance_conflicts is None


@pytest.mark.parametrize("edge", ["start", "end"])
def test_calendar_must_cover_the_entire_requested_window(availability_state, edge):
    requested = query()
    known = calendar(
        start=requested.start_at + timedelta(microseconds=1) if edge == "start" else NOW,
        end=requested.end_at - timedelta(microseconds=1) if edge == "end" else None,
    )
    result = service(availability_state, maintenance=(known,)).assess(requested)
    assert result.code == "NOT_EVALUATED" and result.reason == "MAINTENANCE_NOT_COVERED"
    assert result.available is None


@pytest.mark.parametrize(
    ("status", "blocked"),
    [
        ("confirmed", True),
        ("active", True),
        ("reclaiming", True),
        ("pending", False),
        ("completed", False),
        ("cancelled", False),
        ("rejected", False),
    ],
)
def test_only_occupying_reservations_block_the_window(availability_state, status, blocked):
    sessions, _, owner, _ = availability_state
    with sessions.begin() as session:
        session.add(reservation(owner, status=status))
    result = service(availability_state).assess(query())
    assert result.code == ("CONFLICT" if blocked else "AVAILABLE")
    assert result.available is (not blocked)
    assert result.reservation_conflicts.total == int(blocked)
    assert owner not in result.model_dump_json()
    assert "Private purpose" not in result.model_dump_json()


def test_adjacent_half_open_windows_and_other_gpu_do_not_conflict(availability_state):
    sessions, _, owner, _ = availability_state
    requested = query()
    with sessions.begin() as session:
        session.add_all(
            [
                reservation(owner, start=NOW, end=requested.start_at),
                reservation(owner, start=requested.end_at),
                reservation(owner, gpu="another-GPU"),
            ]
        )
    assert service(availability_state).assess(requested).available is True


def test_one_microsecond_overlap_is_a_real_conflict(availability_state):
    sessions, _, owner, _ = availability_state
    requested = query()
    with sessions.begin() as session:
        session.add(
            reservation(owner, start=NOW, end=requested.start_at + timedelta(microseconds=1))
        )
    result = service(availability_state).assess(requested)
    assert result.code == "CONFLICT" and result.reservation_conflicts.total == 1


def test_conflict_preview_is_bounded_sorted_and_counted_without_private_fields(availability_state):
    sessions, _, owner, _ = availability_state
    start = NOW + timedelta(hours=1)
    with sessions.begin() as session:
        session.add_all(
            [
                reservation(
                    owner, start=start + timedelta(minutes=i), end=start + timedelta(minutes=i + 1)
                )
                for i in reversed(range(27))
            ]
        )
    result = service(availability_state).assess(query(start, start + timedelta(hours=1)))
    summary = result.reservation_conflicts
    assert summary.total == 27 and len(summary.windows) == 20 and summary.truncated
    assert [w.start_at for w in summary.windows] == [
        start + timedelta(minutes=i) for i in range(20)
    ]
    assert all(w.start_at.tzinfo == UTC for w in summary.windows)
    assert (
        owner not in result.model_dump_json() and "Private purpose" not in result.model_dump_json()
    )


@pytest.mark.parametrize("edge", ["before", "after", "overlap"])
def test_maintenance_observes_the_same_half_open_boundaries(availability_state, edge):
    requested = query()
    down = AvailabilityWindow(
        start_at=NOW
        if edge == "before"
        else requested.end_at
        if edge == "after"
        else requested.start_at,
        end_at=requested.start_at if edge == "before" else requested.end_at + timedelta(hours=1),
    )
    result = service(availability_state, maintenance=(calendar(down),)).assess(requested)
    assert result.available is (edge != "overlap")
    assert result.maintenance_conflicts.total == int(edge == "overlap")
    if edge == "overlap":
        assert result.code == "POLICY_REJECTED" and result.reason == "MAINTENANCE_OVERLAP"


def test_both_reservation_and_maintenance_conflicts_are_reported(availability_state):
    sessions, _, owner, _ = availability_state
    requested = query()
    with sessions.begin() as session:
        session.add(reservation(owner))
    down = AvailabilityWindow(start_at=requested.start_at, end_at=requested.end_at)
    result = service(availability_state, maintenance=(calendar(down),)).assess(requested)
    assert result.code == "CONFLICT"
    assert result.reservation_conflicts.total == result.maintenance_conflicts.total == 1


@pytest.mark.parametrize(
    ("requested", "reason"),
    [
        (query(NOW + timedelta(seconds=59, microseconds=999999)), "NOTICE_TOO_SHORT"),
        (query(end=NOW + timedelta(hours=5, microseconds=1)), "WINDOW_TOO_LONG"),
        (query(NOW + timedelta(days=7, microseconds=1)), "TOO_FAR_AHEAD"),
    ],
)
def test_explicit_rule_boundaries_reject_outside_values(availability_state, requested, reason):
    result = service(availability_state).assess(requested)
    assert (
        result.code == "POLICY_REJECTED" and result.available is False and result.reason == reason
    )
    assert result.reservation_conflicts is None


@pytest.mark.parametrize(
    "requested",
    [
        query(NOW + timedelta(seconds=60)),
        query(end=NOW + timedelta(hours=5)),
        query(NOW + timedelta(days=7)),
    ],
)
def test_rule_boundary_values_are_inclusive(availability_state, requested):
    assert service(availability_state).assess(requested).available is True


@pytest.mark.parametrize("invalid", ["duration", "notice", "horizon", "boolean", "extra"])
def test_basic_rules_require_explicit_valid_values_and_a_stable_content_hash(invalid):
    values = RULES.model_dump()
    if invalid == "duration":
        values["max_duration_seconds"] = 0
    elif invalid == "notice":
        values["min_notice_seconds"] = -1
    elif invalid == "horizon":
        values["max_advance_seconds"] = 0
    elif invalid == "boolean":
        values["min_notice_seconds"] = False
    else:
        values["invented_quota"] = 100
    with pytest.raises(ValidationError):
        BasicReservationRules(**values)
    assert (
        BasicReservationRules(**dict(reversed(list(RULES.model_dump().items())))).fingerprint
        == RULES.fingerprint
    )
    assert len(RULES.fingerprint) == 64


@pytest.mark.parametrize(
    "condition", ["node_stale", "offline", "sample_stale", "sample_missing", "error", "future"]
)
def test_unreliable_resource_facts_are_unknown_instead_of_available(availability_state, condition):
    sessions, _, _, _ = availability_state
    changes = {
        "node_stale": {"observed_at": NOW - timedelta(seconds=60)},
        "offline": {"observed_at": NOW - timedelta(seconds=180)},
        "sample_stale": {"telemetry_at": NOW - timedelta(seconds=60)},
        "sample_missing": {"telemetry_at": None},
        "error": {"collector_status": "error"},
        "future": {"observed_at": NOW + timedelta(seconds=301)},
    }[condition]
    with sessions.begin() as session:
        session.execute(update(Node).values(**changes))
    result = service(availability_state).assess(query())
    assert result.code == "STALE_DATA" and result.available is None
    assert result.reservation_conflicts is result.maintenance_conflicts is None


def test_absent_and_unknown_targets_do_not_become_available(availability_state):
    sessions, _, _, _ = availability_state
    assert service(availability_state).assess(query(gpu="unknown")).reason == "TARGET_UNKNOWN"
    with sessions.begin() as session:
        session.execute(update(GpuDevice).values(present=False))
    result = service(availability_state).assess(query())
    assert result.reason == "TARGET_ABSENT" and result.available is False


@pytest.mark.parametrize("stale_component", ["node", "GPU"])
def test_an_old_absent_inventory_is_unknown_not_a_current_absence(
    availability_state, stale_component
):
    sessions, _, _, _ = availability_state
    with sessions.begin() as session:
        session.execute(update(GpuDevice).values(present=False))
        field = "observed_at" if stale_component == "node" else "telemetry_at"
        session.execute(update(Node).values({field: NOW - timedelta(seconds=180)}))
    result = service(availability_state).assess(query())
    assert result.code == "STALE_DATA" and result.available is None


def test_maintenance_preview_is_bounded_and_the_decision_is_scoped(availability_state):
    start = NOW + timedelta(hours=1)
    windows = tuple(
        AvailabilityWindow(
            start_at=start + timedelta(minutes=i), end_at=start + timedelta(minutes=i + 1)
        )
        for i in reversed(range(25))
    )
    result = service(availability_state, maintenance=(calendar(*windows),)).assess(query())
    assert result.assessment_scope == "resource_window"
    assert result.code == "POLICY_REJECTED"
    assert result.maintenance_conflicts.total == 25
    assert result.maintenance_conflicts.truncated
    assert len(result.maintenance_conflicts.windows) == 20


def test_cross_target_contract_error_fails_closed(availability_state):
    sessions, targets, _, _ = availability_state
    original = targets.gpu_in_transaction

    def wrong_target(session, target):
        return original(session, target).model_copy(update={"gpu_uuid": "another-GPU"})

    targets.gpu_in_transaction = wrong_target
    with pytest.raises(DomainError) as error:
        service(availability_state).assess(query())
    assert error.value.code == "INTERNAL_ERROR"
    with sessions() as session:
        assert session.scalars(select(Reservation)).all() == []


def test_query_uses_caller_transaction_without_autoflush_commit_or_extra_session(
    availability_state,
):
    sessions, _, _, engine = availability_state
    statements = []

    def observe(_c, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with sessions.begin() as session:
            pending = User(id=str(uuid4()), display_name="Pending", status="active", timezone="UTC")
            session.add(pending)
            result = service(availability_state).assess_in_transaction(session, query())
            assert result.available is True and pending in session.new and session.in_transaction()
            assert len(statements) == 2 and all(
                s.lstrip().startswith("SELECT ") for s in statements
            )
            session.rollback()
        with sessions() as session:
            assert session.get(User, pending.id) is None
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_existing_orm_reservation_identity_cannot_hide_a_changed_status(availability_state):
    sessions, _, owner, _ = availability_state
    stored = reservation(owner)
    with sessions.begin() as session:
        session.add(stored)
    with sessions() as session:
        cached = session.get(Reservation, stored.id)
        assert cached.status == "confirmed"
        with sessions.begin() as writer:
            writer.execute(update(Reservation).values(status="cancelled"))
        result = service(availability_state).assess_in_transaction(session, query())
        assert result.available is True and cached.status == "confirmed"


def test_non_utc_query_and_postgres_result_preserve_the_requested_instants(availability_state):
    sessions, _, owner, engine = availability_state
    requested = query()
    shifted = timezone(timedelta(hours=9))
    with sessions.begin() as session:
        session.add(reservation(owner))
    with sessions.begin() as session:
        if engine.dialect.name == "postgresql":
            session.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        result = service(availability_state).assess_in_transaction(
            session,
            query(requested.start_at.astimezone(shifted), requested.end_at.astimezone(shifted)),
        )
    assert result.code == "CONFLICT"
    assert result.query.start_at == requested.start_at
    assert result.reservation_conflicts.windows[0].start_at == requested.start_at
    assert result.reservation_conflicts.windows[0].start_at.tzinfo == UTC


def test_invalid_clock_and_invalid_maintenance_do_not_silently_assume_availability(
    availability_state,
):
    with pytest.raises(ValueError, match="timezone"):
        service(availability_state, clock=lambda: NOW.replace(tzinfo=None)).assess(query())
    with pytest.raises(ValueError, match="duplicate"):
        service(availability_state, maintenance=(calendar(), calendar()))
    with pytest.raises(ValidationError):
        calendar(
            AvailabilityWindow(start_at=NOW - timedelta(seconds=1), end_at=NOW + timedelta(hours=1))
        )
    assert json.loads(query().model_dump_json())["start_at"].endswith("Z")


def test_read_decision_inputs_cannot_be_mutated_after_assessment(availability_state):
    result = service(availability_state).assess(query())
    with pytest.raises(ValidationError):
        result.resource.present = False
    with pytest.raises(ValidationError):
        result.rules_snapshot.max_duration_seconds = 1
    with pytest.raises(ValidationError):
        result.query.start_at = NOW
    assert result.available is True and result.resource.present


def test_assessment_works_in_an_actual_postgres_read_only_transaction(availability_state):
    sessions, _, _, engine = availability_state
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only transaction enforcement")
    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    ) as connection:
        with connection.begin(), sessions(bind=connection) as session:
            result = service(availability_state).assess_in_transaction(session, query())
            assert result.available is True
            assert connection.scalar(text("SHOW transaction_read_only")) == "on"
