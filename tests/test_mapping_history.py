from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.admin_schemas import AssignNodeUser, RemoveNodeUser, UpdateUser
from aris.core.access.history_repo import MappingHistoryRepository
from aris.core.access.history_schemas import (
    HistoricalMappingValue,
    HistoricalNodeUserQuery,
    MappingHistoryEntry,
    mapping_history_fingerprint,
)
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.access.models import AccessDirectoryState, NodeUserMapping, NodeUserMappingHistory
from aris.core.access.schemas import NodeUserLookup
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from tests.test_administration import (
    NOW,
    PROFILE,
    admin_state,
    batch,
    context,
    initialized,
    member_id,
    register,
)

__all__ = ["admin_state"]


@pytest.fixture
def history_state(admin_state):
    service, sessions, engine = admin_state
    clock = {"now": NOW}
    service.clock = lambda: clock["now"]
    actor = initialized(admin_state)
    users = [member_id(register(service, actor, member=name)) for name in ("UFIRST", "USECOND")]
    history = NodeUserHistoryService(sessions, clock=lambda: clock["now"])
    return service, sessions, engine, actor, users, clock, history


def assign(state, seconds, user=0, *, expected=None, username="linux-user", node="node-a"):
    service, _, _, actor, users, clock, _ = state
    clock["now"] = NOW + timedelta(seconds=seconds)
    return service.apply(
        context(actor),
        batch(
            AssignNodeUser(
                node_id=node, os_username=username, user_id=users[user], expected_user_id=expected
            )
        ),
    )


def remove(state, seconds, user=0):
    service, _, _, actor, users, clock, _ = state
    clock["now"] = NOW + timedelta(seconds=seconds)
    return service.apply(
        context(actor),
        batch(
            RemoveNodeUser(node_id="node-a", os_username="linux-user", expected_user_id=users[user])
        ),
    )


def query(start, end, *, revision=None, node="node-a", username="linux-user", as_of=200):
    return HistoricalNodeUserQuery(
        node_id=node,
        os_username=username,
        start_at=NOW + timedelta(seconds=start),
        end_at=NOW + timedelta(seconds=end),
        as_of=NOW + timedelta(seconds=as_of),
        through_revision=revision,
    )


def read(state, start, end, **kwargs):
    *_, clock, history = state
    clock["now"] = NOW + timedelta(seconds=200)
    return history.resolve(query(start, end, **kwargs))


def counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (NodeUserMappingHistory, DomainEvent, OutboxEntry, AuditEntry)
        )


def test_assign_records_immutable_execution_time_and_identity_event_evidence(history_state):
    service, sessions, _, actor, users, clock, history = history_state
    before = counts(sessions)
    result = assign(history_state, 10)
    with sessions() as session:
        entry = MappingHistoryRepository(session).latest("node-a", "linux-user")
        domain = session.get(DomainEvent, str(entry.event_id))
        assert entry.before is None and str(entry.after.user_id) == users[0]
        assert entry.effective_at == NOW + timedelta(seconds=10)
        assert entry.revision == result.revision == domain.aggregate_version
        assert domain.event_type == "core.identity.changed" and entry.actor_id == actor
        assert mapping_history_fingerprint(entry) == entry.fingerprint
    assert counts(sessions) == tuple(value + 1 for value in before)
    resolved = read(history_state, 10, 20)
    assert resolved.status == "resolved" and str(resolved.user_id) == users[0]
    assert resolved.evidence[0].id == entry.id and resolved.directory_revision == result.revision


def test_current_reassignment_does_not_rewrite_historical_users_and_boundaries_are_half_open(
    history_state,
):
    _, _, _, _, users, _, _ = history_state
    assign(history_state, 10)
    assign(history_state, 20, 1, expected=users[0])
    assert str(read(history_state, 10, 20).user_id) == users[0]
    assert str(read(history_state, 20, 25).user_id) == users[1]
    crossing = read(history_state, 19, 21)
    assert crossing.status == "needs_review" and crossing.user_id is None
    assert crossing.reason == "MAPPING_CHANGED_DURING_INTERVAL" and len(crossing.evidence) == 2


def test_removal_is_explicitly_unmapped_without_erasing_past_mapping(history_state):
    *_, users, clock, history = history_state
    assign(history_state, 10)
    remove(history_state, 20)
    assert str(read(history_state, 10, 20).user_id) == users[0]
    missing = read(history_state, 20, 25)
    assert missing.status == "unknown" and missing.reason == "EXPLICITLY_UNMAPPED"
    assert read(history_state, 15, 25).status == "needs_review"


def test_legacy_current_mapping_and_an_unchanged_assignment_do_not_invent_a_valid_from(
    history_state,
):
    service, sessions, _, actor, users, clock, history = history_state
    with sessions.begin() as session:
        session.add(
            NodeUserMapping(
                node_id="node-a",
                os_username="linux-user",
                user_id=users[0],
                confidence="verified",
                source="manual",
            )
        )
    before = counts(sessions)
    result = assign(history_state, 10)
    assert not result.changed and counts(sessions)[0] == 0
    assert counts(sessions)[1:3] == before[1:3]
    assert read(history_state, 0, 15).reason == "HISTORY_DOES_NOT_COVER_INTERVAL"
    assign(history_state, 20, 1, expected=users[0])
    assert read(history_state, 0, 10).status == "unknown"
    assert str(read(history_state, 20, 25).user_id) == users[1]


def test_atomic_batch_records_net_mapping_change_instead_of_unobservable_intermediate_values(
    history_state,
):
    service, sessions, _, actor, users, clock, history = history_state
    assign(history_state, 10)
    before = counts(sessions)[0]
    clock["now"] = NOW + timedelta(seconds=20)
    service.apply(
        context(actor),
        batch(
            RemoveNodeUser(node_id="node-a", os_username="linux-user", expected_user_id=users[0]),
            AssignNodeUser(node_id="node-a", os_username="linux-user", user_id=users[0]),
        ),
    )
    assert counts(sessions)[0] == before
    assert str(read(history_state, 10, 25).user_id) == users[0]


def test_equal_timestamp_changes_are_ordered_by_revision_and_old_cutoff_is_reproducible(
    history_state,
):
    _, _, _, _, users, _, _ = history_state
    first = assign(history_state, 10)
    assign(history_state, 10, 1, expected=users[0])
    latest = read(history_state, 10, 20)
    old = read(history_state, 10, 20, revision=first.revision)
    assert str(latest.user_id) == users[1] and str(old.user_id) == users[0]
    assert old.directory_revision == first.revision


def test_time_rollback_rolls_back_mapping_revision_and_all_evidence(history_state):
    service, sessions, _, _, users, clock, _ = history_state
    result = assign(history_state, 20)
    before = counts(sessions)
    with pytest.raises(DomainError) as error:
        assign(history_state, 10, 1, expected=users[0])
    assert error.value.code == "CONFLICT" and counts(sessions) == before
    with sessions() as session:
        assert session.get(NodeUserMapping, ("node-a", "linux-user")).user_id == users[0]
        assert session.get(AccessDirectoryState, "primary").revision == result.revision


def test_mapping_and_identity_event_roll_back_if_history_append_fails(history_state):
    _, sessions, engine, _, _, _, _ = history_state
    before = counts(sessions)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO node_user_mapping_history" in statement:
            raise RuntimeError("fixture history failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            assign(history_state, 10)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(sessions) == before
    with sessions() as session:
        assert session.get(NodeUserMapping, ("node-a", "linux-user")) is None
    assert assign(history_state, 10).changed


def test_suspended_current_user_does_not_become_unknown_in_past_history(history_state):
    service, sessions, _, actor, users, clock, _ = history_state
    assign(history_state, 10)
    clock["now"] = NOW + timedelta(seconds=20)
    service.apply(
        context(actor), batch(UpdateUser(user_id=users[0], profile=PROFILE, status="suspended"))
    )
    assert str(read(history_state, 10, 15).user_id) == users[0]
    assert (
        AccessService(sessions).resolve_node_user(
            NodeUserLookup(node_id="node-a", os_username="linux-user")
        )
        is None
    )


def test_dirty_tracked_projection_and_history_do_not_pollute_the_historical_read(history_state):
    _, sessions, _, _, users, clock, history = history_state
    assign(history_state, 10)
    clock["now"] = NOW + timedelta(seconds=200)
    with sessions() as session:
        mapping = session.get(NodeUserMapping, ("node-a", "linux-user"))
        record = session.scalars(select(NodeUserMappingHistory)).one()
        mapping.user_id, record.fingerprint = users[1], "f" * 64
        result = history.resolve_in_transaction(session, query(10, 20))
        assert str(result.user_id) == users[0] and mapping.user_id == users[1]
        session.rollback()


def test_out_of_band_projection_change_is_unavailable_and_not_silently_repaired(history_state):
    _, sessions, _, _, users, _, _ = history_state
    assign(history_state, 10)
    with sessions.begin() as session:
        session.get(NodeUserMapping, ("node-a", "linux-user")).user_id = users[1]
    with pytest.raises(DomainError) as error:
        read(history_state, 10, 20)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    with pytest.raises(DomainError):
        assign(history_state, 30, 0, expected=users[1])


def test_future_cutoff_and_unknown_history_never_resolve_from_current_mapping(history_state):
    _, _, _, _, _, _, history = history_state
    result = history.resolve(query(0, 10))
    assert result.status == "unknown" and result.reason == "AS_OF_NOT_REACHED"
    assert read(history_state, 0, 10).reason == "HISTORY_DOES_NOT_COVER_INTERVAL"
    assign(history_state, 10)
    with pytest.raises(DomainError) as error:
        read(history_state, 10, 20, revision=1000000)
    assert error.value.code == "CONFLICT"


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE node_user_mapping_history SET reason='changed'",
        "DELETE FROM node_user_mapping_history",
    ],
)
def test_mapping_history_is_immutable_at_the_database_layer(history_state, operation):
    _, sessions, _, _, _, _, _ = history_state
    assign(history_state, 10)
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(text(operation))
    assert counts(sessions)[0] == 1


def test_empty_migration_roundtrip_keeps_legacy_projection_and_does_not_backfill(history_state):
    _, sessions, engine, _, users, _, _ = history_state
    with sessions.begin() as session:
        session.add(
            NodeUserMapping(
                node_id="node-a",
                os_username="linux-user",
                user_id=users[0],
                confidence="verified",
                source="manual",
            )
        )
    config = Config("alembic.ini")
    command.downgrade(config, "0014")
    assert "node_user_mapping_history" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    assert counts(sessions)[0] == 0 and read(history_state, 0, 10).status == "unknown"


def test_nonempty_mapping_history_prevents_downgrade_without_data_loss(history_state):
    _, sessions, _, _, users, _, _ = history_state
    assign(history_state, 10)
    before = counts(sessions)
    with pytest.raises(RuntimeError, match="mapping history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0014")
    assert counts(sessions) == before and str(read(history_state, 10, 20).user_id) == users[0]


def test_history_window_size_is_bounded_without_silently_truncating_changes(history_state):
    _, _, _, _, users, _, _ = history_state
    assign(history_state, 0)
    for i in range(1, 130):
        assign(history_state, i, i % 2, expected=users[(i - 1) % 2])
    with pytest.raises(DomainError, match="budget"):
        read(history_state, 0, 150)


def test_unverified_history_remains_unresolved(history_state):
    _, sessions, _, actor, users, clock, _ = history_state
    with sessions.begin() as session:
        domain = session.scalars(
            select(DomainEvent).order_by(DomainEvent.aggregate_version.desc())
        ).first()
        revision = session.get(AccessDirectoryState, "primary").revision
        value = HistoricalMappingValue(user_id=users[0], confidence="unverified", source="fixture")
        session.add(
            NodeUserMapping(
                node_id="node-a",
                os_username="linux-user",
                user_id=users[0],
                confidence="unverified",
                source="fixture",
            )
        )
        entry = MappingHistoryEntry(
            id=uuid4(),
            revision=revision,
            effective_at=NOW,
            node_id="node-a",
            os_username="linux-user",
            before=None,
            after=value,
            event_id=domain.event_id,
            actor_id=actor,
            operation_id=uuid4(),
            request_id="fixture-history",
            reason="Fixture unverified history",
            fingerprint="0" * 64,
        )
        MappingHistoryRepository(session).append(
            entry.model_copy(update={"fingerprint": mapping_history_fingerprint(entry)})
        )
    result = read(history_state, 0, 10)
    assert result.status == "needs_review" and result.reason == "UNVERIFIED_MAPPING"


def test_postgresql_history_read_only_and_timezone_independent(history_state):
    _, sessions, engine, _, users, clock, history = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL identity history READ ONLY and timezone contract")
    assign(history_state, 10)
    clock["now"] = NOW + timedelta(seconds=200)
    before = counts(sessions)
    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))
        result = history.resolve_in_transaction(session, query(10, 20))
    assert str(result.user_id) == users[0] and counts(sessions) == before


def test_postgresql_directory_change_during_lookup_requires_retry(history_state, monkeypatch):
    service, sessions, engine, actor, users, clock, history = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL real concurrent identity history change")
    assign(history_state, 10)
    clock["now"] = NOW + timedelta(seconds=200)
    original = MappingHistoryRepository.changes

    def changed(repo, selection, bound):
        result = original(repo, selection, bound)
        service.apply(
            context(actor),
            batch(
                AssignNodeUser(
                    node_id="node-a",
                    os_username="linux-user",
                    user_id=users[1],
                    expected_user_id=users[0],
                )
            ),
        )
        return result

    monkeypatch.setattr(MappingHistoryRepository, "changes", changed)
    with pytest.raises(DomainError) as error:
        history.resolve(query(10, 20))
    assert error.value.code == "CONFLICT"


def test_postgresql_mapping_race_has_one_atomic_history_entry(history_state):
    service, sessions, engine, actor, users, clock, _ = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL mapping history directory lock race")
    with sessions() as session:
        revision = session.get(AccessDirectoryState, "primary").revision
    barrier = Barrier(2)

    def change(user):
        barrier.wait(timeout=10)
        try:
            return service.apply(
                context(actor),
                batch(
                    AssignNodeUser(node_id="node-a", os_username="linux-user", user_id=user),
                    revision=revision,
                ),
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(change, users))
    assert result.count("CONFLICT") == 1 and counts(sessions)[0] == 1


def test_postgresql_truncate_is_rejected(history_state):
    _, sessions, engine, _, _, _, _ = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL mapping history TRUNCATE guard")
    assign(history_state, 10)
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(text("TRUNCATE node_user_mapping_history"))
