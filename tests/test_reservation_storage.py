import runpy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command, op
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from aris.core.access.models import User
from aris.core.db import build_engine, session_factory
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.schemas import ReservationWindow

AT = datetime(2026, 12, 1, 10, tzinfo=UTC)


@pytest.fixture
def reservation_state(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    owners = [str(uuid4()), str(uuid4())]
    with sessions.begin() as db:
        db.add_all(
            [
                User(id=value, display_name="Fixture owner", status="active", timezone="UTC")
                for value in owners
            ]
        )
    try:
        yield sessions, owners
    finally:
        engine.dispose()


def row(owner, gpu="GPU-fixture-a", start=AT, end=None, status="confirmed"):
    return Reservation(
        id=str(uuid4()),
        owner_id=owner,
        gpu_uuid=gpu,
        start_at=start,
        end_at=end or start + timedelta(hours=1),
        status=status,
        purpose="explicit storage fixture",
        policy_version="fixture-policy",
        created_at=AT,
        updated_at=AT,
    )


def postgres_only(sessions):
    with sessions() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL range exclusion constraints")


def exclusion(error):
    assert error.value.orig.sqlstate == "23P01"
    assert error.value.orig.diag.constraint_name == "ex_reservation_gpu_interval"


def test_windows_require_timezone_normalize_utc_and_have_positive_duration():
    shifted = AT.astimezone(timezone(timedelta(hours=2)))
    window = ReservationWindow(start_at=shifted, end_at=shifted + timedelta(hours=1))
    assert window.start_at == AT and window.start_at.tzinfo == UTC
    for start, end in [
        (AT, AT),
        (AT, AT - timedelta(seconds=1)),
        (AT.replace(tzinfo=None), AT + timedelta(hours=1)),
    ]:
        with pytest.raises(ValidationError):
            ReservationWindow(start_at=start, end_at=end)


@pytest.mark.parametrize("invalid", ["empty", "reverse", "status", "owner"])
def test_database_row_checks_and_owner_reference_are_enforced(reservation_state, invalid):
    sessions, owners = reservation_state
    item = row(owners[0])
    if invalid == "empty":
        item.end_at = item.start_at
    elif invalid == "reverse":
        item.end_at = item.start_at - timedelta(seconds=1)
    elif invalid == "status":
        item.status = "invented_status"
    else:
        item.owner_id = str(uuid4())
    with pytest.raises(IntegrityError), sessions.begin() as db:
        db.add(item)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0


def test_internal_overlap_query_observes_half_open_boundaries_and_blocking_states(
    reservation_state,
):
    sessions, owners = reservation_state
    first, second = row(owners[0]), row(owners[0], start=AT + timedelta(hours=1), status="active")
    with sessions.begin() as db:
        db.add_all(
            [first, second, row(owners[0], status="cancelled"), row(owners[1], gpu="GPU-fixture-b")]
        )
    with sessions() as db:
        repo = ReservationRepository(db)
        assert [
            item.id
            for item in repo.overlapping(
                "GPU-fixture-a",
                ReservationWindow(
                    start_at=AT + timedelta(minutes=30), end_at=AT + timedelta(hours=1)
                ),
            )
        ] == [first.id]
        assert [
            item.id
            for item in repo.overlapping(
                "GPU-fixture-a",
                ReservationWindow(
                    start_at=AT + timedelta(hours=1), end_at=AT + timedelta(minutes=90)
                ),
            )
        ] == [second.id]
        assert (
            repo.overlapping(
                "GPU-fixture-a", ReservationWindow(start_at=AT - timedelta(hours=1), end_at=AT)
            )
            == []
        )


@pytest.mark.parametrize("status", ["confirmed", "active", "reclaiming"])
def test_postgres_all_occupying_states_exclude_overlapping_gpu_windows(reservation_state, status):
    sessions, owners = reservation_state
    postgres_only(sessions)
    with sessions.begin() as db:
        db.add(row(owners[0], status=status))
    with pytest.raises(IntegrityError) as error, sessions.begin() as db:
        db.add(row(owners[1], start=AT + timedelta(minutes=30)))
    exclusion(error)


@pytest.mark.parametrize("status", ["pending", "completed", "cancelled", "rejected"])
def test_postgres_nonoccupying_states_do_not_prevent_a_new_booking(reservation_state, status):
    sessions, owners = reservation_state
    postgres_only(sessions)
    with sessions.begin() as db:
        db.add_all([row(owners[0], status=status), row(owners[1])])
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 2


def test_postgres_adjacent_windows_and_independent_gpus_can_coexist(reservation_state):
    sessions, owners = reservation_state
    postgres_only(sessions)
    with sessions.begin() as db:
        db.add_all(
            [
                row(owners[0]),
                row(owners[1], start=AT + timedelta(hours=1)),
                row(owners[1], gpu="GPU-fixture-b"),
            ]
        )
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 3


@pytest.mark.parametrize("change", ["status", "window", "gpu"])
def test_postgres_updates_cannot_bypass_the_exclusion_constraint(reservation_state, change):
    sessions, owners = reservation_state
    postgres_only(sessions)
    first = row(owners[0])
    second = (
        row(owners[1], status="pending")
        if change == "status"
        else row(owners[1], start=AT + timedelta(hours=1))
        if change == "window"
        else row(owners[1], gpu="GPU-fixture-b")
    )
    with sessions.begin() as db:
        db.add_all([first, second])
    with pytest.raises(IntegrityError) as error, sessions.begin() as db:
        item = db.get(Reservation, second.id)
        if change == "status":
            item.status = "active"
        elif change == "window":
            item.start_at = AT + timedelta(minutes=30)
        else:
            item.gpu_uuid = "GPU-fixture-a"
    exclusion(error)


def test_postgres_timezone_offsets_do_not_create_a_second_slot_for_the_same_instant(
    reservation_state,
):
    sessions, owners = reservation_state
    postgres_only(sessions)
    with sessions.begin() as db:
        db.add(row(owners[0]))
    same_instant = AT.astimezone(timezone(timedelta(hours=2)))
    with pytest.raises(IntegrityError) as error, sessions.begin() as db:
        db.add(row(owners[1], start=same_instant))
    exclusion(error)


def test_postgres_two_concurrent_overlaps_can_commit_only_one_row(reservation_state):
    sessions, owners = reservation_state
    postgres_only(sessions)
    start = Barrier(2)

    def insert(owner):
        start.wait(timeout=10)
        try:
            with sessions.begin() as db:
                db.add(row(owner))
            return "committed"
        except DBAPIError as error:
            # PostgreSQL may detect an exclusion conflict or choose a victim
            # of concurrent exclusion-check deadlock. Both roll back fully;
            # the future service must map deadlock to retry, not fake success.
            assert error.orig.sqlstate in {"23P01", "40P01"}
            return error.orig.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert, owners))
    assert outcomes.count("committed") == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1


def test_postgres_constraint_uses_the_current_schema_range_and_expected_operator_class(
    reservation_state,
):
    sessions, _ = reservation_state
    postgres_only(sessions)
    with sessions() as db:
        definition, opclass, namespace = db.execute(
            text(
                "SELECT pg_get_constraintdef(c.oid),opc.opcname,n.nspname "
                "FROM pg_constraint c JOIN pg_index i ON i.indexrelid=c.conindid "
                "JOIN pg_opclass opc ON opc.oid=i.indclass[0] "
                "JOIN pg_namespace n ON n.oid=opc.opcnamespace "
                "WHERE c.conrelid=to_regclass('reservations') AND c.contype='x' "
                "AND c.conname='ex_reservation_gpu_interval'"
            )
        ).one()
    assert "EXCLUDE USING gist" in definition and "tstzrange" in definition and "[)" in definition
    assert all(status in definition for status in ("confirmed", "active", "reclaiming"))
    assert "pending" not in definition and (opclass, namespace) == ("gist_text_ops", "public")


def test_reservation_event_version_is_unique_and_positive(reservation_state):
    sessions, owners = reservation_state
    item = row(owners[0])
    with sessions.begin() as db:
        db.add(item)

    def entry(version):
        return ReservationEvent(
            id=str(uuid4()),
            reservation_id=item.id,
            version=version,
            event_type="fixture.confirmed",
            actor=owners[0],
            occurred_at=AT,
            payload={"explicit_fixture": True},
        )

    with sessions.begin() as db:
        db.add(entry(1))
    for version in (1, 0, -1):
        with pytest.raises(IntegrityError), sessions.begin() as db:
            db.add(entry(version))
    with sessions.begin() as db:
        db.add(entry(2))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ReservationEvent)) == 2


def test_0009_empty_roundtrip_and_drift_do_not_change_existing_platform_rows(reservation_state):
    sessions, owners = reservation_state
    config = Config("alembic.ini")
    command.downgrade(config, "0008")
    with sessions() as db:
        assert list(db.scalars(select(User.id).order_by(User.id))) == sorted(owners)
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        assert list(db.scalars(select(User.id).order_by(User.id))) == sorted(owners)


def test_0009_refuses_downgrade_when_reservation_history_exists(reservation_state):
    sessions, owners = reservation_state
    # Exercise this migration at its own revision, independently of later guards
    # and each database's transactional-DDL rollback behavior.
    command.downgrade(Config("alembic.ini"), "0009")
    with sessions.begin() as db:
        db.add(row(owners[0]))
    with pytest.raises(RuntimeError, match="reservation history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0008")
    with sessions() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))


def test_0009_requires_extension_before_creating_tables_and_does_not_install_it(monkeypatch):
    migration = runpy.run_path("migrations/versions/0009_reservation_storage.py")
    queries = []

    def scalar(query):
        queries.append(str(query))
        return None

    monkeypatch.setattr(
        op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), scalar=scalar),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("no table DDL before dependency check")

    monkeypatch.setattr(op, "create_table", forbidden)
    with pytest.raises(RuntimeError, match="pre-provisioned public.btree_gist"):
        migration["upgrade"]()
    assert len(queries) == 1 and queries[0].startswith("SELECT 1 FROM pg_catalog.pg_extension")
