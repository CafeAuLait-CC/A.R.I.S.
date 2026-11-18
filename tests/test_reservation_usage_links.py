import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text

from aris.core.contracts import DomainError
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.usage_link_repo import ReservationUsageLinkRepository
from aris.modules.nova.usage_link_schemas import ReservationUsageQuery
from aris.modules.nova.usage_link_service import ReservationUsageLinkService
from tests.test_gpu_target_facts import NOW
from tests.test_reservation_cancellation import cancelled, cancelling
from tests.test_reservation_extension import extend, extending, fresh_fixture
from tests.test_reservation_release import release, releasing
from tests.test_reservation_service import booking, counts, create

__all__ = ["booking", "cancelling", "extending", "releasing"]


def query(state, reservation_id, *, version=1, start=70, end=80, as_of=240, **kwargs):
    return ReservationUsageQuery(
        reservation_id=reservation_id,
        expected_version=version,
        owner_id=state[3]["alice"],
        gpu_uuid="GPU-target",
        start_at=NOW + timedelta(minutes=start),
        end_at=NOW + timedelta(minutes=end),
        as_of=NOW + timedelta(minutes=as_of),
        **kwargs,
    )


def reader(state):
    return ReservationUsageLinkService(state[2], clock=lambda: NOW + timedelta(hours=4))


def test_explicit_reservation_links_only_its_owner_gpu_and_fully_covered_window(booking):
    created = create(booking).result["reservation"]
    before = counts(booking)
    result = reader(booking).resolve(query(booking, created["id"]))
    assert result.state == "matched" and result.query.expected_version == 1
    assert len(result.evidence) == 1 and result.evidence[0].event_type == "reservation.created"
    assert result.history_fingerprint and counts(booking) == before
    assert "Private fixture purpose" not in result.model_dump_json()
    assert "reason" not in result.evidence[0].model_dump()
    assert reader(booking).resolve(
        result.query.model_copy(update={"expected_history_fingerprint": result.history_fingerprint})
    ) == result.model_copy(
        update={
            "query": result.query.model_copy(
                update={"expected_history_fingerprint": result.history_fingerprint}
            )
        }
    )


@pytest.mark.parametrize("kind", ["owner", "gpu", "before", "after", "overlap"])
def test_explicit_reference_does_not_guess_or_partially_link_a_different_interval(booking, kind):
    created = create(booking).result["reservation"]
    request = query(booking, created["id"])
    changes = {
        "owner": {"owner_id": uuid4()},
        "gpu": {"gpu_uuid": "GPU-other"},
        "before": {"start_at": NOW + timedelta(minutes=59)},
        "after": {"end_at": NOW + timedelta(minutes=121)},
        "overlap": {
            "start_at": NOW + timedelta(minutes=119),
            "end_at": NOW + timedelta(minutes=121),
        },
    }[kind]
    result = reader(booking).resolve(request.model_copy(update=changes))
    assert result.state == "not_matched"
    assert result.reason == (
        "OWNER_OR_RESOURCE_DIFFERS" if kind in {"owner", "gpu"} else "WINDOW_NOT_COVERED"
    )


def test_reservation_boundaries_are_half_open_and_as_of_is_explicit(booking):
    created = create(booking).result["reservation"]
    assert (
        reader(booking).resolve(query(booking, created["id"], start=60, end=120)).state == "matched"
    )
    assert (
        reader(booking).resolve(query(booking, created["id"], start=120, end=121)).state
        == "not_matched"
    )
    with pytest.raises(ValidationError):
        query(booking, created["id"], as_of=79)


def test_unknown_and_future_references_remain_unknown_without_claiming_unreserved(booking):
    service = reader(booking)
    assert service.resolve(query(booking, uuid4())).reason == "RESERVATION_NOT_FOUND"
    service.clock = lambda: NOW
    assert service.resolve(query(booking, uuid4())).reason == "AS_OF_NOT_REACHED"


def test_no_history_before_creation_is_not_a_claim_of_unreserved_use(booking):
    created = create(booking).result["reservation"]
    result = reader(booking).resolve(query(booking, created["id"], start=-10, end=-5, as_of=-1))
    assert result.state == "unknown" and result.reason == "NO_RESERVATION_AT_CUTOFF"


def test_cancelled_reservation_does_not_supply_actual_use_or_coverage(cancelling):
    created = create(cancelling).result["reservation"]
    cancelled(cancelling, created["id"])
    result = reader(cancelling).resolve(query(cancelling, created["id"], version=2))
    assert result.state == "not_matched" and result.reason == "CANCELLED_RESERVATION"
    with pytest.raises(DomainError) as error:
        reader(cancelling).resolve(query(cancelling, created["id"]))
    assert error.value.code == "CONFLICT"


def test_release_preserves_original_plan_but_links_only_until_effective_end(releasing):
    created = create(releasing).result["reservation"]
    releasing[4]["now"] = NOW + timedelta(minutes=80)
    release(releasing, created["id"])
    service = reader(releasing)
    assert (
        service.resolve(query(releasing, created["id"], version=2, start=60, end=80)).state
        == "matched"
    )
    assert (
        service.resolve(query(releasing, created["id"], version=2, end=81)).reason
        == "WINDOW_NOT_COVERED"
    )
    old = service.resolve(query(releasing, created["id"], start=60, end=70, as_of=75))
    assert old.state == "matched" and len(old.evidence) == 1


def test_later_extension_does_not_change_an_earlier_cutoff_or_old_fingerprint(extending):
    created = create(extending).result["reservation"]
    fresh_fixture(extending, NOW + timedelta(minutes=90))
    extend(extending, created["id"])
    result = reader(extending).resolve(
        query(extending, created["id"], version=2, start=130, end=140)
    )
    assert result.state == "matched" and len(result.evidence) == 2
    earlier = reader(extending).resolve(query(extending, created["id"], start=60, end=70, as_of=75))
    assert earlier.state == "matched" and len(earlier.evidence) == 1
    with pytest.raises(DomainError) as error:
        reader(extending).resolve(
            query(extending, created["id"], version=2, expected_history_fingerprint="f" * 64)
        )
    assert error.value.code == "CONFLICT"


@pytest.mark.parametrize(
    "field", ["node_id", "version", "previous_end_at", "end_at", "assessment_scope"]
)
def test_extension_metadata_cannot_claim_another_scope_or_window(extending, field):
    created = create(extending).result["reservation"]
    extend(extending, created["id"])
    with extending[2].begin() as session:
        history = session.scalars(
            select(ReservationEvent).where(ReservationEvent.version == 2)
        ).one()
        body = copy.deepcopy(history.payload)
        body[field] = 99 if field == "version" else "inconsistent-fixture-metadata"
        history.payload = body
    with pytest.raises(DomainError) as error:
        reader(extending).resolve(query(extending, created["id"], version=2))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_history_budget_is_enforced_before_returning_a_truncated_association(booking):
    created = create(booking).result["reservation"]
    with booking[2].begin() as session:
        origin = session.scalars(select(ReservationEvent)).one()
        for version in range(2, 130):
            session.add(
                ReservationEvent(
                    id=str(uuid4()),
                    reservation_id=origin.reservation_id,
                    version=version,
                    event_type="reservation.extended",
                    actor=origin.actor,
                    occurred_at=origin.occurred_at,
                    payload=copy.deepcopy(origin.payload),
                )
            )
    with pytest.raises(DomainError, match="budget"):
        reader(booking).resolve(query(booking, created["id"]))


@pytest.mark.parametrize(
    "kind", ["missing_origin", "owner", "projection", "time", "version", "command"]
)
def test_broken_history_is_unavailable_instead_of_being_guessed(booking, kind):
    created = create(booking).result["reservation"]
    with booking[2].begin() as session:
        row = session.get(Reservation, created["id"])
        history = session.scalars(select(ReservationEvent)).one()
        if kind == "missing_origin":
            session.execute(delete(ReservationEvent))
        elif kind == "owner":
            row.owner_id = booking[3]["bob"]
        elif kind == "projection":
            row.end_at += timedelta(minutes=1)
        elif kind == "time":
            history.occurred_at += timedelta(seconds=1)
        elif kind == "version":
            history.version = 2
        else:
            payload = copy.deepcopy(history.payload)
            payload["command_id"] = "invalid"
            history.payload = payload
    with pytest.raises(DomainError) as error:
        reader(booking).resolve(query(booking, created["id"]))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_dirty_tracked_projection_and_history_are_not_flushed_or_used(booking):
    created = create(booking).result["reservation"]
    with booking[2]() as session:
        row = session.get(Reservation, created["id"])
        source = session.scalars(select(ReservationEvent)).one()
        row.owner_id = booking[3]["bob"]
        source.payload = {"invalid": "dirty fixture"}
        result = reader(booking).resolve_in_transaction(session, query(booking, created["id"]))
        assert result.state == "matched"
        session.rollback()


def test_historical_source_fingerprint_detects_changed_content_at_the_same_version(booking):
    created = create(booking).result["reservation"]
    original = reader(booking).resolve(query(booking, created["id"]))
    with booking[2].begin() as session:
        history = session.scalars(select(ReservationEvent)).one()
        payload = copy.deepcopy(history.payload)
        payload["request_id"] = "another-fixture-request"
        history.payload = payload
    request = original.query.model_copy(
        update={"expected_history_fingerprint": original.history_fingerprint}
    )
    with pytest.raises(DomainError) as error:
        reader(booking).resolve(request)
    assert error.value.code == "CONFLICT"


def test_postgresql_association_is_read_only_and_uses_two_snapshot_reads(booking):
    if booking[-1].dialect.name != "postgresql":
        pytest.skip("PostgreSQL reservation usage association READ ONLY and budget")
    created = create(booking).result["reservation"]
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with booking[2].begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        event.listen(booking[-1], "before_cursor_execute", record)
        try:
            result = reader(booking).resolve_in_transaction(session, query(booking, created["id"]))
        finally:
            event.remove(booking[-1], "before_cursor_execute", record)
    assert result.state == "matched" and len(statements) == 2


def test_postgresql_concurrent_history_mutation_invalidates_association(booking, monkeypatch):
    if booking[-1].dialect.name != "postgresql":
        pytest.skip("PostgreSQL reservation association concurrent history change")
    created = create(booking).result["reservation"]
    original = ReservationUsageLinkRepository.history
    calls = []

    def changed(repo, target):
        result = original(repo, target)
        if not calls:
            calls.append(True)
            with booking[2].begin() as session:
                history = session.scalars(select(ReservationEvent)).one()
                payload = copy.deepcopy(history.payload)
                payload["request_id"] = "concurrent-fixture-request"
                history.payload = payload
        return result

    monkeypatch.setattr(ReservationUsageLinkRepository, "history", changed)
    with pytest.raises(DomainError) as error:
        reader(booking).resolve(query(booking, created["id"]))
    assert error.value.code == "CONFLICT"
