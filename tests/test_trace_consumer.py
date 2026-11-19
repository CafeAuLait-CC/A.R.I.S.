import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import UsageLedgerEntryRow, UsageTraceCursorRow, UsageTraceRecordRow
from aris.modules.lyra.residency_sessions import ResidencyFormationRule
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    StoredTraceConsumption,
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
    UsageTraceConsumeRequest,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from aris.modules.vega.usage_service import VegaUsageObservations
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_resource_telemetry import put, resource_batch

CONTEXT = UsageTraceConsumeContext(request_id="fixture-candidate-processing")


def configuration(**changes):
    return UsageTraceConfiguration.model_validate(
        {
            "consumer_id": "fixture-node-a-candidate-trace",
            "source_ref": "fixture-explicit-trace-configuration",
            "agent_id": "agent-a",
            "node_id": "node-a",
            "initial_after_seq": 0,
            "continuity": UsageContinuityRule(max_gap_seconds=45),
            "formation": ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30),
            **changes,
        }
    )


def rebuild(state, *, config=None):
    return UsageTraceConsumerService(
        state.sessions,
        state.archive,
        state.observations,
        configuration=config or state.config,
        clock=state.now,
    )


@pytest.fixture
def consumer(client):
    sessions = sessionmaker(client.app.state.engine, expire_on_commit=False)
    clock = {"at": None}

    def now():
        return clock["at"] or datetime.now(UTC)

    state = SimpleNamespace(
        client=client,
        sessions=sessions,
        config=configuration(),
        base=datetime.now(UTC) - timedelta(minutes=10),
        clock=clock,
        now=now,
        archive=VegaUsageArchive(sessions, clock=now),
        observations=VegaUsageObservations(sessions),
    )
    state.service = rebuild(state)
    return state


def seed(state, times=(10, 25, 40), *, skip=(), ages=None):
    result = []
    for index, seconds in enumerate(times):
        seq = index * 2 + 1
        payload = ownership(
            resource_batch(
                seq=seq,
                observed_at=state.base + timedelta(seconds=seconds),
                compute_age=ages[index] if ages else 0,
            ),
            [owner()],
        )
        if seq not in skip:
            assert put(state.client, payload).status_code == 200
        if seq + 1 not in skip:
            assert beat(state.client, heartbeat(seq=seq + 1)).status_code == 200
        result.append(payload)
    return result


def request(state, **changes):
    return UsageTraceConsumeRequest.model_validate(
        {
            "id": uuid4(),
            "expected_configuration_fingerprint": state.config.fingerprint,
            "expected_revision": state.service.position().revision,
            "through_seq": 6,
            **changes,
        }
    )


def counts(state):
    with state.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                UsageTraceRecordRow,
                UsageTraceCursorRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
                UsageLedgerEntryRow,
            )
        )


def test_original_prefix_receipt_cursor_event_and_replay_are_one_candidate_operation(consumer):
    state = consumer
    seed(state)
    before = counts(state)
    incoming = request(state)
    result = state.service.consume(CONTEXT, incoming)
    assert result.recorded and not result.duplicate and result.candidate_only
    assert (
        not result.formal_usage_available
        and result.source_count == 6
        and result.telemetry_count == 3
    )
    assert result.position.revision == 1 and result.position.after_seq == 6
    assert result.position.trace.last.reference.seq == 5 and result.page_state == "range_end"
    assert result.closed_run_count == 0 and result.open_run_count == 1
    assert result.position.residency.open_runs[0].intervals == 2
    assert result.position.residency.open_runs[0].observed_microseconds == 30000000
    assert counts(state) == tuple(x + y for x, y in zip(before, (1, 1, 1, 1, 1, 0), strict=True))
    stored = state.service.exact(incoming.id)
    assert stored.position == result.position and stored.context == CONTEXT
    assert "resources" not in stored.model_dump_json()
    assert all("fixture-user" not in item.model_dump_json() for item in stored.sources)
    assert stored.after_residency.open_runs[0].username == "fixture-user"
    with state.sessions() as session:
        public_event = session.get(DomainEvent, str(result.event_id))
        audit = session.scalar(
            select(AuditEntry).where(AuditEntry.action == "lyra.usage.trace_consumed")
        )
        assert "fixture-user" not in json.dumps(public_event.payload)
        assert "fixture-user" not in json.dumps(audit.changes)
    after = counts(state)
    state.service.configuration = None
    replay = state.service.consume(
        UsageTraceConsumeContext(request_id="fixture-restarted-worker"), incoming
    )
    assert replay.duplicate and replay.model_copy(update={"duplicate": False}) == result
    assert counts(state) == after


def test_restart_after_every_page_matches_one_complete_candidate_stream(consumer):
    state = consumer
    seed(state)
    result = None
    for _ in range(3):
        incoming = request(state, limit=2)
        result = state.service.consume(CONTEXT, incoming)
        state.service = rebuild(state)
        assert state.service.position() == result.position
    entire = rebuild(state, config=configuration(consumer_id="fixture-second-candidate-stream"))
    complete = entire.consume(
        CONTEXT,
        UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=entire.configuration.fingerprint,
            expected_revision=0,
            through_seq=6,
        ),
    )
    assert result.position.trace == complete.position.trace
    assert result.position.residency == complete.position.residency
    assert counts(state)[0] == 4 and counts(state)[1] == 2 and counts(state)[-1] == 0


def test_cached_compute_reload_uses_the_original_anchor_instead_of_last_envelope(
    consumer, monkeypatch
):
    state = consumer
    seed(state, ages=(0, 15, 0))
    first = state.service.consume(CONTEXT, request(state, through_seq=4))
    assert first.position.trace.anchor.reference.seq == 1
    assert first.position.trace.last.reference.seq == 3
    state.service = rebuild(state)
    original = state.observations.observation_in_transaction
    lookups = []

    def capture(session, query):
        lookups.append(query.expected_seq)
        return original(session, query)

    monkeypatch.setattr(state.observations, "observation_in_transaction", capture)
    second = state.service.consume(CONTEXT, request(state))
    assert second.position.trace.anchor.reference.seq == 5
    assert set(lookups) == {1}


def test_clock_rollback_quarantine_survives_database_checkpoint_restarts(consumer):
    state = consumer
    seed(state, times=(10, 5, 7, 11, 26))
    first = state.service.consume(CONTEXT, request(state, through_seq=4))
    assert first.position.trace.blocked_at == state.base + timedelta(seconds=10)
    state.service = rebuild(state)
    second = state.service.consume(CONTEXT, request(state, through_seq=6))
    assert second.position.trace.blocked_at == first.position.trace.blocked_at
    state.service = rebuild(state)
    third = state.service.consume(CONTEXT, request(state, through_seq=8))
    assert (
        third.position.trace.blocked_at is None and third.position.trace.anchor.reference.seq == 7
    )
    final = state.service.consume(CONTEXT, request(state, through_seq=10))
    assert final.position.trace.anchor.reference.seq == 9 and not final.formal_usage_available


def test_gap_prefix_and_no_progress_do_not_skip_or_create_empty_poll_records(consumer):
    state = consumer
    seed(state, skip=(2,))
    first = state.service.consume(CONTEXT, request(state))
    assert first.recorded and first.position.after_seq == 1 and first.missing_seq == 2
    before = counts(state)
    waiting = request(state)
    idle = state.service.consume(CONTEXT, waiting)
    assert not idle.recorded and idle.state == "gap" and idle.position == first.position
    assert counts(state) == before
    assert beat(state.client, heartbeat(seq=2)).status_code == 200
    filled = state.service.consume(CONTEXT, waiting)
    assert filled.recorded and filled.position.after_seq == 6 and filled.position.revision == 2
    before = counts(state)
    caught = state.service.consume(CONTEXT, request(state))
    assert not caught.recorded and caught.state == "caught_up" and counts(state) == before


def test_heartbeat_only_progress_never_creates_a_compute_anchor_or_usage(consumer):
    state = consumer
    for seq in (1, 2, 3):
        assert beat(state.client, heartbeat(seq=seq)).status_code == 200
    result = state.service.consume(CONTEXT, request(state, through_seq=3))
    assert result.recorded and result.source_count == 3 and result.telemetry_count == 0
    assert result.position.trace is None and counts(state)[-1] == 0
    assert (
        result.position.residency is None and result.open_run_count == result.closed_run_count == 0
    )


def test_idle_transition_closes_one_persisted_run_without_using_the_page_boundary(consumer):
    state = consumer
    seed(state)
    for _ in range(3):
        first = state.service.consume(CONTEXT, request(state, limit=2))
        assert first.closed_run_count == 0
        state.service = rebuild(state)
    original = first.position.residency.open_runs[0]
    idle = resource_batch(seq=7, observed_at=state.base + timedelta(seconds=55))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(state.client, idle).status_code == 200
    assert beat(state.client, heartbeat(seq=8)).status_code == 200
    final = state.service.consume(CONTEXT, request(state, through_seq=8))
    assert final.closed_run_count == 1 and final.open_run_count == 0
    saved = state.service.exact(final.id)
    candidate = saved.closed_runs[0]
    assert candidate.run == original and candidate.state == "stable_candidate"
    assert candidate.boundary_reason == "USAGE_TRANSITION"
    assert candidate.run.last.seq == 5 and counts(state)[-1] == 0


def test_saved_record_cannot_drop_a_previously_open_run_even_with_a_new_content_hash(consumer):
    state = consumer
    seed(state)
    state.service.consume(CONTEXT, request(state, through_seq=4))
    second = state.service.consume(CONTEXT, request(state))
    saved = state.service.exact(second.id)
    altered = saved.model_copy(
        update={"after_residency": saved.after_residency.model_copy(update={"open_runs": ()})}
    )
    raw = altered.model_dump(mode="json")
    raw["fingerprint"] = trace_fingerprint(
        "record", canonical_trace(altered, exclude={"fingerprint"})
    )
    with pytest.raises(ValidationError):
        StoredTraceConsumption.model_validate(raw)


def test_residency_processing_budget_refuses_a_large_batch_without_advancing_cursor(
    consumer, monkeypatch
):
    state = consumer
    seed(state)
    before, incoming = counts(state), request(state)
    with monkeypatch.context() as patch:
        patch.setattr("aris.modules.lyra.trace_consumer_service.MAX_TRACE_RECORD_BYTES", 1)
        with pytest.raises(DomainError) as error:
            state.service.consume(CONTEXT, incoming)
    assert error.value.code == "USAGE_PROCESSING_TOO_LARGE" and counts(state) == before
    assert state.service.position().revision == 0
    assert not state.service.consume(CONTEXT, incoming).duplicate


def test_stale_revision_changed_key_input_and_reconfigured_stream_never_reset_progress(consumer):
    state = consumer
    seed(state)
    incoming = request(state)
    first = state.service.consume(CONTEXT, incoming)
    before = counts(state)
    for changed in (
        incoming.model_copy(update={"id": uuid4()}),
        incoming.model_copy(update={"through_seq": 5}),
    ):
        with pytest.raises(DomainError) as error:
            state.service.consume(CONTEXT, changed)
        assert error.value.code == "CONFLICT"
    state.service.configuration = configuration(continuity=UsageContinuityRule(max_gap_seconds=30))
    with pytest.raises(DomainError) as error:
        state.service.position()
    assert error.value.code == "CONFLICT" and counts(state) == before
    state.service.configuration = None
    with pytest.raises(DomainError) as error:
        state.service.consume(
            CONTEXT, incoming.model_copy(update={"id": uuid4(), "expected_revision": 1})
        )
    assert error.value.code == "USAGE_NOT_CONFIGURED"
    state.service = rebuild(state)
    assert state.service.position() == first.position


def test_missing_cursor_is_unavailable_instead_of_restarting_at_configured_initial_sequence(
    consumer,
):
    state = consumer
    seed(state)
    result = state.service.consume(CONTEXT, request(state))
    with state.sessions.begin() as session:
        session.execute(delete(UsageTraceCursorRow))
    with pytest.raises(DomainError) as error:
        state.service.position()
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert state.service.exact(result.id).position == result.position


@pytest.mark.parametrize(
    "table", ["domain_events", "usage_trace_records", "usage_trace_cursors", "event_outbox"]
)
def test_write_interruption_rolls_back_progress_and_all_related_effects(consumer, table):
    state = consumer
    seed(state)
    before, incoming = counts(state), request(state)

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table):
            raise RuntimeError("fixture interrupted candidate processing")

    engine = state.client.app.state.engine
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            state.service.consume(CONTEXT, incoming)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(state) == before and state.service.position().revision == 0
    assert not state.service.consume(CONTEXT, incoming).duplicate


def test_final_source_verification_failure_keeps_the_original_cursor(consumer, monkeypatch):
    state = consumer
    seed(state)
    before, incoming = counts(state), request(state)
    verify = state.service._verify_sources
    calls = []

    def changed(session, record):
        calls.append(record.request.id)
        if len(calls) == 2:
            raise DomainError("CONFLICT", "fixture source changed after provisional write", 409)
        return verify(session, record)

    monkeypatch.setattr(state.service, "_verify_sources", changed)
    with pytest.raises(DomainError) as error:
        state.service.consume(CONTEXT, incoming)
    assert error.value.code == "CONFLICT" and counts(state) == before


def test_missing_original_anchor_never_falls_back_to_current_snapshot_or_resets_trace(
    consumer, monkeypatch
):
    state = consumer
    seed(state)
    first = state.service.consume(CONTEXT, request(state, through_seq=2))
    incoming, before = request(state), counts(state)
    monkeypatch.setattr(state.observations, "observation_in_transaction", lambda *args: None)
    with pytest.raises(DomainError) as error:
        state.service.consume(CONTEXT, incoming)
    assert error.value.code == "CONFLICT" and counts(state) == before
    assert state.service.position() == first.position


def test_configuration_pause_after_provisional_append_rolls_back_cursor_and_history(
    consumer, monkeypatch
):
    state = consumer
    seed(state)
    before, incoming = counts(state), request(state)
    append = state.service._append

    def paused(repo, record):
        append(repo, record)
        state.service.configuration = None

    monkeypatch.setattr(state.service, "_append", paused)
    with pytest.raises(DomainError) as error:
        state.service.consume(CONTEXT, incoming)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(state) == before


def test_outer_transaction_can_roll_back_a_provisional_processing_receipt(consumer):
    state = consumer
    seed(state)
    before, incoming = counts(state), request(state)
    with state.sessions() as session:
        assert state.service.consume_in_transaction(session, CONTEXT, incoming).recorded
        session.rollback()
    assert counts(state) == before and state.service.position().revision == 0
    assert state.service.consume(CONTEXT, incoming).recorded


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_acknowledgement_recovers_with_the_original_operation_key(
    consumer, monkeypatch, when
):
    import aris.modules.lyra.trace_consumer_service as module

    state = consumer
    seed(state)
    incoming = request(state)
    original = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            original(session)
        raise RuntimeError("fixture unknown candidate commit acknowledgement")

    monkeypatch.setattr(module, "commit_owned_transaction", interrupted)
    with pytest.raises(RuntimeError):
        state.service.consume(CONTEXT, incoming)
    monkeypatch.setattr(module, "commit_owned_transaction", original)
    retry = state.service.consume(CONTEXT, incoming)
    assert retry.duplicate is (when == "after")
    assert counts(state)[:2] == (1, 1) and state.service.position().revision == 1


def test_immutable_processing_history_blocks_changes_and_nonempty_downgrade(consumer):
    state = consumer
    seed(state)
    incoming = request(state)
    first = state.service.consume(CONTEXT, incoming)
    for sql in (
        "DELETE FROM usage_trace_records",
        "UPDATE usage_trace_records SET fingerprint='changed'",
    ):
        with pytest.raises(DBAPIError), state.sessions.begin() as session:
            session.execute(text(sql))
    with pytest.raises(RuntimeError, match="candidate trace history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0017")
    assert state.service.consume(CONTEXT, incoming).position == first.position


def test_empty_processing_migration_roundtrip_has_no_drift_or_source_changes(consumer):
    state = consumer
    seed(state)
    before = counts(state)
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0017")
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert counts(state) == before
    assert state.service.consume(CONTEXT, request(state)).recorded


def test_processing_inputs_cannot_supply_checkpoint_owner_actor_or_complete_usage():
    config = configuration()
    raw = dict(
        id=uuid4(),
        expected_configuration_fingerprint=config.fingerprint,
        expected_revision=0,
        through_seq=1,
    )
    for change in (
        {"after_seq": 100},
        {"checkpoint": None},
        {"owner_id": str(uuid4())},
        {"actor": "system:lyra"},
        {"formal_usage_available": True},
        {"limit": 129},
        {"expected_revision": True},
    ):
        with pytest.raises(ValidationError):
            UsageTraceConsumeRequest.model_validate({**raw, **change})
    with pytest.raises(ValidationError):
        UsageTraceConsumeContext(actor="fixture-user", request_id="fixture")


def test_saved_candidate_record_rejects_scope_authority_and_fingerprint_tampering(consumer):
    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    original = state.service.exact(receipt.id)
    for change in (
        {"formal_usage_available": True},
        {"fingerprint": "f" * 64},
        {"missing_seq": 2},
        {"after_trace": None},
    ):
        with pytest.raises(ValidationError):
            StoredTraceConsumption.model_validate({**original.model_dump(), **change})


def postgres(state):
    if state.client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL durable candidate processing concurrency")


@pytest.mark.parametrize("same_key", [True, False])
def test_postgresql_competing_consumers_record_one_committed_prefix(consumer, same_key):
    state = consumer
    postgres(state)
    seed(state)
    incoming = request(state)
    other = incoming if same_key else incoming.model_copy(update={"id": uuid4()})
    gate = Barrier(2)

    def run(value):
        gate.wait(timeout=5)
        try:
            return state.service.consume(CONTEXT, value)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (incoming, other)))
    assert counts(state)[:2] == (1, 1) and state.service.position().revision == 1
    if same_key:
        assert sorted(result.duplicate for result in results) == [False, True]
    else:
        assert results.count("CONFLICT") == 1


def test_postgresql_waiting_consumer_rechecks_configuration_before_progress(consumer, monkeypatch):
    state = consumer
    postgres(state)
    seed(state)
    incoming = request(state)
    waiting = Event()
    original = UsageTraceRepository.lock

    def capture(repo, kind, key):
        if kind == "consumer":
            waiting.set()
        return original(repo, kind, key)

    with state.sessions() as held:
        original(UsageTraceRepository(held), "consumer", state.config.consumer_id)
        monkeypatch.setattr(UsageTraceRepository, "lock", capture)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(state.service.consume, CONTEXT, incoming)
            assert waiting.wait(timeout=2)
            state.service.configuration = None
            held.rollback()
            with pytest.raises(DomainError) as error:
                future.result(timeout=5)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(state)[:2] == (0, 0)


def test_postgresql_uncommitted_candidate_cursor_is_not_visible_and_history_is_read_only(consumer):
    state = consumer
    postgres(state)
    seed(state)
    incoming = request(state)
    with state.sessions.begin() as writer:
        provisional = state.service.consume_in_transaction(writer, CONTEXT, incoming)
        assert provisional.position.revision == 1 and state.service.position().revision == 0
    assert state.service.position() == provisional.position
    with state.sessions() as reader:
        reader.execute(text("SET TRANSACTION READ ONLY"))
        assert UsageTraceRepository(reader).get(incoming.id).position == provisional.position
        assert UsageTraceRepository(reader).position(state.config) == provisional.position
    with pytest.raises(DBAPIError), state.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_trace_records CASCADE"))


def test_postgresql_consumer_gate_timeout_preserves_request_and_initial_cursor(consumer):
    state = consumer
    postgres(state)
    seed(state)
    incoming = request(state)
    before = counts(state)
    with state.sessions() as held:
        UsageTraceRepository(held).lock("consumer", state.config.consumer_id)
        with pytest.raises(DomainError) as error:
            state.service.consume(CONTEXT, incoming)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR" and counts(state) == before
        held.rollback()
    assert not state.service.consume(CONTEXT, incoming).duplicate


def test_postgresql_position_metadata_lock_wait_is_bounded_and_recoverable(consumer):
    state = consumer
    postgres(state)
    with state.sessions.begin() as holder:
        holder.execute(text("LOCK TABLE usage_trace_cursors IN ACCESS EXCLUSIVE MODE"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(state.service.position)
            with pytest.raises(DomainError) as error:
                future.result(timeout=5)
            assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert state.service.position().revision == 0
