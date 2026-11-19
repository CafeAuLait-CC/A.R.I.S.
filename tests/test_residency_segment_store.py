from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.modules.lyra.models import UsageResidencySegmentRow, UsageResidencySegmentSetRow
from aris.modules.lyra.segment_store_schemas import (
    RecordResidencySegments,
    ResidencySegmentLookup,
    ResidencySegmentPage,
)
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.vega.models import AgentMessage
from tests.test_residency_segments import analyzer, query
from tests.test_trace_attestations import attested, incoming, service, snapshot
from tests.test_trace_consumer import CONTEXT, consumer, postgres, request, seed
from tests.test_usage_ledger import POST

__all__ = ["attested", "consumer"]


def store(state):
    return ResidencySegmentStore(state.sessions, analyzer(state), clock=state.now)


def record_request(proof):
    return RecordResidencySegments(id=uuid4(), source=query(proof))


def lookup(value, **changes):
    segment = value.analysis.segments[0]
    return ResidencySegmentLookup.model_validate(
        dict(
            consumer_id=value.analysis.attestation.consumer_id,
            run_id=segment.run_at_segment_end.id,
            after_interval=0,
            through_interval=segment.run_at_segment_end.intervals,
            **changes,
        )
    )


def counts(state):
    with state.sessions() as session:
        extra = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (UsageResidencySegmentSetRow, UsageResidencySegmentRow)
        )
    return (*snapshot(state), *extra)


@pytest.fixture
def saved(attested):
    state, _, proofs = attested
    values = [
        store(state).record(POST, record_request(value.attestation)).value for value in proofs
    ]
    return state, values


@pytest.fixture
def wide(consumer):
    state = consumer
    seed(state, times=tuple(10 + 15 * i for i in range(10)))
    receipt = state.service.consume(CONTEXT, request(state, through_seq=20))
    proof = service(state).record(CONTEXT, incoming(state, receipt)).attestation
    return state, store(state).record(POST, record_request(proof)).value


def test_fixed_segment_sets_indexes_and_replay_preserve_original_read_only_candidates(saved):
    state, values = saved
    before = counts(state)
    assert before[-2:] == (3, 2)
    for value in values:
        assert store(state).exact(value.request.id) == value
        assert store(state).reverify(value.request.id) == value
        replay = store(state).record(POST, value.request)
        assert replay.duplicate and replay.value == value
        assert not value.quota_usage_available
    assert counts(state) == before
    assert values[0].analysis.segments == ()
    for changed in (
        values[-1].request.model_copy(update={"source": values[0].request.source}),
        values[-1].request.model_copy(update={"id": uuid4()}),
    ):
        with pytest.raises(DomainError) as error:
            store(state).record(POST, changed)
        assert error.value.code == "CONFLICT"


def test_bounded_pages_cover_offsets_without_claiming_original_revalidation_or_usage(saved):
    state, values = saved
    value = values[-1]
    page = store(state).lookup(lookup(value).model_copy(update={"limit": 1}))
    assert (
        page.state == "page_end" and page.next_after_interval == 1 and page.missing_interval is None
    )
    last = store(state).lookup(page.query.model_copy(update={"after_interval": 1}))
    assert last.state == "range_end" and last.next_after_interval == 2
    assert page.items[0].segment.last == last.items[0].segment.first
    assert page.original_source_revalidation == "not_performed" and not page.quota_usage_available
    assert store(state).lookup(page.query).fingerprint == page.fingerprint
    absent = store(state).lookup(page.query.model_copy(update={"run_id": "f" * 64}))
    assert absent.state == "gap" and absent.missing_interval == 0 and absent.items == ()


def test_an_incomplete_materialized_prefix_stops_at_its_first_missing_interval(attested):
    state, _, proofs = attested
    later = store(state).record(POST, record_request(proofs[-1].attestation)).value
    missing = store(state).lookup(lookup(later))
    assert (
        missing.state == "gap"
        and missing.missing_interval == 0
        and missing.next_after_interval == 0
    )
    store(state).record(POST, record_request(proofs[1].attestation))
    complete = store(state).lookup(lookup(later))
    assert complete.state == "range_end" and len(complete.items) == 2
    beyond = store(state).lookup(lookup(later).model_copy(update={"through_interval": 3}))
    assert beyond.state == "gap" and beyond.next_after_interval == beyond.missing_interval == 2


def test_lookup_returns_whole_original_segment_when_query_boundaries_are_inside_it(wide):
    state, value = wide
    selected = lookup(value).model_copy(update={"after_interval": 2, "through_interval": 4})
    page = store(state).lookup(selected)
    assert page.state == "range_end" and page.next_after_interval == 4
    segment = page.items[0].segment
    assert segment.interval_offset == 0 and segment.run_at_segment_end.intervals == 9
    assert segment.observed_microseconds == 135000000


def test_saved_history_is_distinct_from_current_source_availability(saved):
    state, values = saved
    value = values[-1]
    with state.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 5))
    before = counts(state)
    assert store(state).exact(value.request.id) == value
    assert store(state).lookup(lookup(value)).state == "range_end"
    with pytest.raises(DomainError):
        store(state).reverify(value.request.id)
    assert counts(state) == before


def test_source_body_budget_is_checked_before_fetching_any_saved_analysis(saved, monkeypatch):
    import aris.modules.lyra.segment_store_repo as module

    state, values = saved
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = state.client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "MAX_SEGMENT_LOOKUP_CHARACTERS", 1)
            with pytest.raises(DomainError) as error:
                store(state).lookup(lookup(values[-1]))
            assert error.value.code == "USAGE_EVIDENCE_TOO_LARGE"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert all(
        "length(" in statement for statement in statements if "canonical_record" in statement
    )


def test_corrupted_index_is_not_used_in_place_of_original_immutable_evidence(saved):
    state, values = saved
    value = values[-1]
    # Deliberate administrative corruption of this test's isolated schema only.
    with state.sessions.begin() as session:
        pg = session.get_bind().dialect.name == "postgresql"
        if pg:
            session.execute(text("ALTER TABLE usage_residency_segments DISABLE TRIGGER USER"))
        else:
            session.execute(text("DROP TRIGGER usage_residency_segments_no_update"))
        session.execute(
            text("UPDATE usage_residency_segments SET last_seq=last_seq+1 WHERE set_id=:id"),
            {"id": str(value.request.id)},
        )
        if pg:
            session.execute(text("ALTER TABLE usage_residency_segments ENABLE TRIGGER USER"))
    assert store(state).exact(value.request.id) == value
    for call in (
        lambda: store(state).lookup(lookup(value)),
        lambda: store(state).reverify(value.request.id),
    ):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_final_original_replay_failure_rolls_back_set_index_and_all_events(attested, monkeypatch):
    state, _, proofs = attested
    app = store(state)
    value = record_request(proofs[-1].attestation)
    before = counts(state)
    original = app.analyzer.analyze_in_transaction
    calls = []

    def changed(*args):
        calls.append(True)
        if len(calls) == 2:
            raise DomainError("CONFLICT", "fixture original changed after provisional index", 409)
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(app.analyzer, "analyze_in_transaction", changed)
        with pytest.raises(DomainError):
            app.record(POST, value)
    assert counts(state) == before and not app.record(POST, value).duplicate


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_reuses_fixed_segment_set_and_index_without_duplicate_rows(
    attested, monkeypatch, when
):
    import aris.modules.lyra.segment_store_service as module

    state, _, proofs = attested
    app = store(state)
    value = record_request(proofs[-1].attestation)
    original = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            original(session)
        raise RuntimeError("fixture unknown saved segment commit")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError):
            app.record(POST, value)
    assert app.record(POST, value).duplicate == (when == "after")
    assert counts(state)[-2:] == (1, 1)


def test_index_insert_failure_and_outer_rollback_never_leave_a_partial_set(attested):
    state, _, proofs = attested
    app = store(state)
    value = record_request(proofs[-1].attestation)
    before = counts(state)

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO usage_residency_segments "):
            raise RuntimeError("fixture index interrupted")

    engine = state.client.app.state.engine
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            app.record(POST, value)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(state) == before
    with state.sessions() as session:
        app.record_in_transaction(session, POST, value)
        session.rollback()
    assert counts(state) == before
    assert not app.record(POST, value).duplicate


def test_migration_roundtrip_and_nonempty_immutable_set_and_index_history(attested):
    state, _, proofs = attested
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0021")
    command.upgrade(cfg, "head")
    command.check(cfg)
    value = store(state).record(POST, record_request(proofs[-1].attestation)).value
    for table in ("usage_residency_segment_sets", "usage_residency_segments"):
        for sql in ("DELETE FROM " + table, "UPDATE " + table + " SET id=id"):
            with pytest.raises(DBAPIError), state.sessions.begin() as session:
                session.execute(text(sql))
    with pytest.raises(RuntimeError, match="residency segment history prevents downgrade"):
        command.downgrade(cfg, "0021")
    assert store(state).reverify(value.request.id) == value


def test_contracts_cannot_supply_values_or_claim_clipped_complete_usage(saved):
    state, values = saved
    value = values[-1]
    page = store(state).lookup(lookup(value))
    for changes in ({"owner_id": str(uuid4())}, {"segments": []}, {"observed_microseconds": 1}):
        with pytest.raises(ValidationError):
            RecordResidencySegments.model_validate({**value.request.model_dump(), **changes})
    for changes in (
        {"quota_usage_available": True},
        {"original_source_revalidation": "verified"},
        {"state": "range_end", "next_after_interval": 99},
    ):
        with pytest.raises(ValidationError):
            ResidencySegmentPage.model_validate({**page.model_dump(), **changes})
    for changes in ({"after_interval": -1}, {"limit": 129}, {"through_interval": 0}):
        with pytest.raises(ValidationError):
            ResidencySegmentLookup.model_validate({**page.query.model_dump(), **changes})


def test_postgresql_read_only_lookup_and_reproof_preserve_budget_and_dirty_orm(saved):
    state, values = saved
    postgres(state)
    app = store(state)
    value = values[-1]
    with state.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        row = session.get(UsageResidencySegmentSetRow, str(value.request.id))
        row.canonical_record = "pending invalid content"

        def reject(*args):
            raise AssertionError("source lookup must not flush owner state")

        event.listen(session, "before_flush", reject)
        try:
            assert app.lookup_in_transaction(session, lookup(value)).state == "range_end"
            assert app.reverify_in_transaction(session, value.request.id) == value
            assert (
                session.scalar(text("SHOW transaction_timeout")) == "2s"
                and session.scalar(text("SHOW statement_timeout")) == "750ms"
            )
        finally:
            event.remove(session, "before_flush", reject)


def test_postgresql_competing_operations_store_one_set_per_attestation(attested):
    state, _, proofs = attested
    postgres(state)
    value = record_request(proofs[-1].attestation)
    gate = Barrier(2)

    def run(_):
        gate.wait(timeout=5)
        return store(state).record(POST, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(result.duplicate for result in results) == [False, True] and counts(state)[
        -2:
    ] == (1, 1)
    with pytest.raises(DomainError) as error:
        store(state).record(POST, value.model_copy(update={"id": uuid4()}))
    assert error.value.code == "CONFLICT"


def test_postgresql_overlapping_interval_index_is_rejected_by_database(wide):
    state, value = wide
    postgres(state)
    with state.sessions() as session:
        row = dict(session.execute(select(*UsageResidencySegmentRow.__table__.c)).mappings().one())
    row.update(id="f" * 64, segment_index=42, interval_offset=2, through_interval=4)
    with pytest.raises(DBAPIError) as error, state.sessions.begin() as session:
        session.add(UsageResidencySegmentRow(**row))
        session.flush()
    assert getattr(error.value.orig, "sqlstate", None) == "23P01"
    assert store(state).lookup(lookup(value)).state == "range_end"


def test_postgresql_uncommitted_index_is_not_visible_and_source_lock_timeout_is_retryable(attested):
    state, _, proofs = attested
    postgres(state)
    app = store(state)
    value = record_request(proofs[-1].attestation)
    with state.sessions() as writer:
        result = app.record_in_transaction(writer, POST, value).value
        assert app.lookup(lookup(result)).state == "gap"
        writer.commit()
    with state.sessions.begin() as held:
        held.execute(text("LOCK TABLE usage_residency_segment_sets IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            app.lookup(lookup(result))
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert app.lookup(lookup(result)).state == "gap"  # Only the later offset was materialized.
    for table in ("usage_residency_segment_sets", "usage_residency_segments"):
        with pytest.raises(DBAPIError), state.sessions.begin() as session:
            session.execute(text("TRUNCATE " + table + " CASCADE"))
