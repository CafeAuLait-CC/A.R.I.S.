from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    UsageLedgerHead,
    UsagePostingConfiguration,
    UsagePostingContext,
    UsagePostingRequest,
    make_ledger_entry,
)
from aris.modules.lyra.ledger_service import UsageLedgerService
from aris.modules.lyra.models import (
    UsageLedgerClaim,
    UsageLedgerEntryRow,
    UsageLedgerOperationRow,
    UsageLedgerState,
)
from aris.modules.vega.usage_schemas import UsageObservationQuery
from tests.test_administration import NOW, admin_state
from tests.test_historical_usage_facts import owner, ownership
from tests.test_mapping_history import history_state, remove
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_derivations import CONTEXT, derivations

__all__ = ["admin_state", "history_state", "derivations"]
POST = UsagePostingContext(request_id="fixture-ledger-post")
TABLES = (
    UsageLedgerEntryRow,
    UsageLedgerClaim,
    UsageLedgerOperationRow,
    UsageLedgerState,
    DomainEvent,
    OutboxEntry,
    AuditEntry,
)


@pytest.fixture
def ledger(derivations, client):
    source, request, state = derivations
    original = source.record(CONTEXT, request)
    configuration = UsagePostingConfiguration(
        source_ref="fixture-explicit-recording-context",
        recording_start_at=NOW,
        timing_fingerprint=source.exact(
            request.id
        ).artifact.analysis.request.attribution.residency.rule_fingerprint,
    )
    service = UsageLedgerService(
        state[1], source, configuration=configuration, clock=lambda: state[5]["now"]
    )
    post = UsagePostingRequest(
        id=uuid4(),
        derivation_id=request.id,
        session_index=0,
        expected_result_fingerprint=original.result_fingerprint,
        expected_configuration_fingerprint=configuration.fingerprint,
    )
    return service, post, derivations, configuration, client


def counts(state):
    with state[2][2][1]() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in TABLES)


def post_window(state, *, start, seq):
    source, original, history = state[2]
    queries = []
    for index in range(3):
        payload = ownership(
            resource_batch(
                seq=seq + index * 2, observed_at=NOW + timedelta(seconds=start + index * 15)
            ),
            [owner(username="linux-user")],
        )
        assert put(state[4], payload).status_code == 200
        queries.append(
            UsageObservationQuery(
                agent_id=payload["agent_id"],
                node_id=payload["node_id"],
                message_id=payload["message_id"],
                expected_seq=payload["seq"],
            )
        )
    cutoff = max(datetime.now(UTC), NOW + timedelta(seconds=start + 60))
    history[5]["now"] = cutoff + timedelta(seconds=1)
    request = original.model_copy(
        update={"id": uuid4(), "sources": tuple(queries), "as_of": cutoff}
    )
    receipt = source.record(CONTEXT, request)
    return state[1].model_copy(
        update={
            "id": uuid4(),
            "derivation_id": request.id,
            "expected_result_fingerprint": receipt.result_fingerprint,
        }
    )


def postgres(state):
    sessions = state[2][2][1]
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL ledger locks/constraints")
    return sessions


def test_measurement_receipt_event_projection_and_same_key_replay_are_atomic(ledger):
    service, request, _, configuration, _ = ledger
    before = counts(ledger)
    first = service.post(POST, request)
    assert first.effect == "recorded" and not first.duplicate and not first.quota_usage_available
    assert first.head.revision == 1
    assert counts(ledger) == tuple(
        a + b for a, b in zip(before, (1, 1, 1, 1, 1, 1, 1), strict=True)
    )
    entry = service.exact(first.entries[0])
    assert isinstance(entry.details, PostedMeasurement)
    assert entry.details.measurement.observed_microseconds == 30000000
    assert entry.details.measurement.source_derivation_id == request.derivation_id
    assert (
        entry.details.configuration == configuration and entry.fingerprint == first.head.fingerprint
    )
    values = dict(
        id=entry.id,
        event_id=entry.event_id,
        node_id=entry.node_id,
        revision=entry.revision,
        previous_fingerprint=entry.previous_fingerprint,
        actor=entry.actor,
        request_id=entry.request_id,
        details=entry.details,
    )
    assert (
        make_ledger_entry(
            **values, recorded_at=entry.recorded_at.astimezone(timezone(timedelta(hours=8)))
        )
        == entry
    )
    with pytest.raises(ValueError, match="timezone"):
        make_ledger_entry(**values, recorded_at=entry.recorded_at.replace(tzinfo=None))
    prior = counts(ledger)
    replay = service.post(UsagePostingContext(request_id="another-fixture-request"), request)
    assert replay.duplicate and replay.model_copy(update={"duplicate": False}) == first
    assert counts(ledger) == prior


def test_recomputed_source_and_new_operation_cannot_charge_the_same_window_twice(ledger):
    first = ledger[0].post(POST, ledger[1])
    source, original, _ = ledger[2]
    other = original.model_copy(update={"id": uuid4()})
    proof = source.record(CONTEXT, other)
    request = ledger[1].model_copy(
        update={
            "id": uuid4(),
            "derivation_id": other.id,
            "expected_result_fingerprint": proof.result_fingerprint,
        }
    )
    before = counts(ledger)
    second = ledger[0].post(POST, request)
    assert second.effect == "already_recorded" and not second.duplicate
    assert second.entries == first.entries and second.head == first.head
    assert counts(ledger) == tuple(
        a + b for a, b in zip(before, (0, 0, 1, 0, 0, 0, 1), strict=True)
    )


def test_reused_operation_id_different_evidence_or_context_never_rewrites_receipt(ledger):
    first = ledger[0].post(POST, ledger[1])
    before = counts(ledger)
    for changes in (
        {"expected_result_fingerprint": "f" * 64},
        {"expected_configuration_fingerprint": "e" * 64},
    ):
        with pytest.raises(DomainError) as error:
            ledger[0].post(POST, ledger[1].model_copy(update=changes))
        assert error.value.code == "CONFLICT"
    assert ledger[0].post(POST, ledger[1]).entries == first.entries and counts(ledger) == before


def test_recording_requires_explicit_context_but_historical_receipt_is_replayable(ledger):
    service = ledger[0]
    configuration = service.configuration
    service.configuration = None
    before = counts(ledger)
    with pytest.raises(DomainError) as error:
        service.post(POST, ledger[1])
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(ledger) == before
    service.configuration = configuration
    first = service.post(POST, ledger[1])
    service.configuration = None
    assert service.post(POST, ledger[1]).model_copy(update={"duplicate": False}) == first
    with pytest.raises(DomainError):
        service.post(POST, ledger[1].model_copy(update={"id": uuid4()}))


def test_recording_start_and_timing_mismatch_never_clip_or_relabel_evidence(ledger):
    service, request, _, configuration, _ = ledger
    before = counts(ledger)
    for changes, code in (
        ({"recording_start_at": NOW + timedelta(seconds=20)}, "USAGE_NEEDS_REVIEW"),
        ({"timing_fingerprint": "e" * 64}, "DEPENDENCY_UNAVAILABLE"),
    ):
        service.configuration = configuration.model_copy(update=changes)
        changed = request.model_copy(
            update={
                "id": uuid4(),
                "expected_configuration_fingerprint": service.configuration.fingerprint,
            }
        )
        with pytest.raises(DomainError) as error:
            service.post(POST, changed)
        assert error.value.code == code and counts(ledger) == before


def test_unresolved_historical_identity_remains_reviewable_without_creating_a_measurement(ledger):
    source, original, history = ledger[2]
    now = history[5]["now"]
    changed = remove(history, 8)
    history[5]["now"] = now
    request = original.model_copy(update={"id": uuid4(), "identity_revision": changed.revision})
    receipt = source.record(CONTEXT, request)
    post = ledger[1].model_copy(
        update={
            "derivation_id": request.id,
            "expected_result_fingerprint": receipt.result_fingerprint,
        }
    )
    before = counts(ledger)
    with pytest.raises(DomainError) as error:
        ledger[0].post(POST, post)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(ledger) == before


def test_final_source_failure_after_flushed_measurement_keeps_the_entire_operation_uncommitted(
    ledger,
):
    original = ledger[0].derivations
    before = counts(ledger)

    class Interrupted:
        calls = 0

        def verified_in_transaction(self, session, record_id):
            self.calls += 1
            if self.calls == 3:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture final source failure", 503)
            return original.verified_in_transaction(session, record_id)

    ledger[0].derivations = Interrupted()
    with pytest.raises(DomainError):
        ledger[0].post(POST, ledger[1])
    assert counts(ledger) == before
    ledger[0].derivations = original
    assert ledger[0].post(POST, ledger[1]).effect == "recorded"


def test_partial_overlap_requires_review_but_adjacent_half_open_window_is_independent(ledger):
    first = ledger[0].post(POST, ledger[1])
    overlap = post_window(ledger, start=25, seq=9)
    before = counts(ledger)
    with pytest.raises(DomainError) as error:
        ledger[0].post(POST, overlap)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(ledger) == before
    adjacent = post_window(ledger, start=40, seq=15)
    second = ledger[0].post(POST, adjacent)
    assert second.effect == "recorded" and second.head.revision == 2
    entry = ledger[0].exact(second.entries[0])
    assert entry.previous_fingerprint == first.head.fingerprint
    assert entry.details.measurement.start_at == NOW + timedelta(seconds=40)


@pytest.mark.parametrize("projection", ["claim", "state"])
def test_missing_or_corrupted_projection_is_unavailable_instead_of_known_empty(ledger, projection):
    ledger[0].post(POST, ledger[1])
    sessions = ledger[2][2][1]
    with sessions.begin() as session:
        if projection == "claim":
            session.execute(delete(UsageLedgerClaim))
        else:
            session.execute(update(UsageLedgerState).values(fingerprint="e" * 64))
    before = counts(ledger)
    with pytest.raises(DomainError) as error:
        ledger[0].post(POST, ledger[1].model_copy(update={"id": uuid4()}))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(ledger) == before


@pytest.mark.parametrize(
    "table",
    [
        "usage_ledger_entries",
        "usage_ledger_claims",
        "usage_ledger_operations",
        "event_outbox",
        "audit_logs",
    ],
)
def test_write_failure_rolls_back_measurement_prefix_receipt_and_all_events(ledger, table):
    engine = ledger[2][2][2]
    before = counts(ledger)

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table):
            raise RuntimeError("fixture atomic write interruption")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            ledger[0].post(POST, ledger[1])
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(ledger) == before
    assert ledger[0].post(POST, ledger[1]).effect == "recorded"


def test_outer_transaction_retains_authority_over_a_provisional_receipt(ledger):
    sessions = ledger[2][2][1]
    before = counts(ledger)
    with sessions() as session:
        result = ledger[0].post_in_transaction(session, POST, ledger[1])
        assert result.effect == "recorded"
        session.rollback()
    assert counts(ledger) == before
    assert ledger[0].post(POST, ledger[1]).effect == "recorded"


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_outcome_uses_same_key_without_false_success_or_duplicate_time(
    ledger, monkeypatch, when
):
    import aris.modules.lyra.ledger_service as module

    commit = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            commit(session)
        raise RuntimeError("fixture unknown commit acknowledgement")

    monkeypatch.setattr(module, "commit_owned_transaction", interrupted)
    with pytest.raises(RuntimeError):
        ledger[0].post(POST, ledger[1])
    monkeypatch.setattr(module, "commit_owned_transaction", commit)
    retry = ledger[0].post(POST, ledger[1])
    assert retry.duplicate is (when == "after")
    assert counts(ledger)[:4] == (1, 1, 1, 1)


def test_entries_and_operation_history_cannot_be_changed_and_nonempty_downgrade_is_refused(ledger):
    first = ledger[0].post(POST, ledger[1])
    sessions = ledger[2][2][1]
    for table in ("usage_ledger_entries", "usage_ledger_operations"):
        for operation in ("UPDATE " + table + " SET id='changed'", "DELETE FROM " + table):
            with pytest.raises(DBAPIError), sessions.begin() as session:
                session.execute(text(operation))
    with pytest.raises(RuntimeError, match="usage ledger history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0016")
    assert ledger[0].post(POST, ledger[1]).model_copy(update={"duplicate": False}) == first


def test_empty_ledger_migration_round_trip_preserves_source_receipts_without_drift(ledger):
    source, original, _ = ledger[2]
    before = source.exact(original.id)
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0016")
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert source.exact(original.id) == before
    assert ledger[0].post(POST, ledger[1]).effect == "recorded"


@pytest.mark.parametrize("same_key", [True, False])
def test_postgresql_concurrent_replay_and_recomputation_produce_one_measurement(ledger, same_key):
    postgres(ledger)
    gate = Barrier(2)

    def run(request):
        gate.wait(timeout=5)
        return ledger[0].post(POST, request)

    other = ledger[1] if same_key else ledger[1].model_copy(update={"id": uuid4()})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (ledger[1], other)))
    assert results[0].entries == results[1].entries
    assert counts(ledger)[:4] == (1, 1, 1 if same_key else 2, 1)
    assert sorted(result.duplicate for result in results) == (
        [False, True] if same_key else [False, False]
    )


def test_postgresql_overlapping_concurrent_candidates_never_both_commit(ledger):
    postgres(ledger)
    other = post_window(ledger, start=25, seq=9)
    gate = Barrier(2)

    def run(request):
        gate.wait(timeout=5)
        try:
            return ledger[0].post(POST, request).effect
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (ledger[1], other)))
    assert sorted(results) == ["USAGE_NEEDS_REVIEW", "recorded"]
    assert counts(ledger)[:4] == (1, 1, 1, 1)


def test_postgresql_database_exclusion_rejects_a_writer_which_skips_the_service(ledger):
    sessions = postgres(ledger)
    first = ledger[0].post(POST, ledger[1])
    entry = ledger[0].exact(first.entries[0])
    measured = entry.details.measurement.model_copy(
        update={"start_at": NOW + timedelta(seconds=20), "end_at": NOW + timedelta(seconds=50)}
    )
    other = make_ledger_entry(
        id=uuid4(),
        event_id=uuid4(),
        node_id=entry.node_id,
        revision=2,
        previous_fingerprint=entry.fingerprint,
        actor=POST.actor,
        request_id=POST.request_id,
        recorded_at=entry.recorded_at,
        details=PostedMeasurement(measurement=measured, configuration=ledger[3]),
    )
    with pytest.raises(DBAPIError) as failure, sessions.begin() as session:
        session.add(
            DomainEvent(
                event_id=str(other.event_id),
                event_type="fixture.ledger",
                event_version=1,
                aggregate_type="usage_ledger",
                aggregate_id=entry.node_id,
                aggregate_version=2,
                occurred_at=entry.recorded_at,
                recorded_at=entry.recorded_at,
                actor=POST.actor,
                correlation_id=POST.request_id,
                causation_id=str(other.id),
                payload={},
            )
        )
        session.flush()
        repo = UsageLedgerRepository(session)
        repo.append(other, first.head)
        session.flush()
        repo.claim(other)
        session.flush()
    assert (
        failure.value.orig.sqlstate == "23P01"
        and failure.value.orig.diag.constraint_name == "ex_usage_ledger_gpu_interval"
    )
    assert counts(ledger)[:4] == (1, 1, 1, 1)


def test_postgresql_waiting_writer_rechecks_changed_recording_configuration(ledger, monkeypatch):
    sessions = postgres(ledger)
    waiting = Event()
    original = UsageLedgerRepository.lock
    with sessions.begin() as holder:
        original(UsageLedgerRepository(holder), "node", "node-a")

        def observed(repo, kind, key):
            if kind == "node":
                waiting.set()
            return original(repo, kind, key)

        monkeypatch.setattr(UsageLedgerRepository, "lock", observed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ledger[0].post, POST, ledger[1])
            assert waiting.wait(timeout=3)
            ledger[0].configuration = None
            holder.commit()
            with pytest.raises(DomainError) as error:
                future.result(timeout=3)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(ledger)[:4] == (0, 0, 0, 0)


def test_postgresql_receipt_reads_are_read_only_and_history_cannot_be_truncated(ledger):
    sessions = postgres(ledger)
    first = ledger[0].post(POST, ledger[1])
    with sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        repo = UsageLedgerRepository(session)
        assert repo.operation(ledger[1].id).receipt.entries == first.entries
        assert repo.head("node-a") == first.head
    for table in ("usage_ledger_entries", "usage_ledger_operations"):
        with pytest.raises(DBAPIError), sessions.begin() as session:
            session.execute(text("TRUNCATE " + table + " CASCADE"))


def test_posting_contract_cannot_accept_caller_owner_window_or_actor():
    base = dict(
        id=uuid4(),
        derivation_id=uuid4(),
        session_index=0,
        expected_result_fingerprint="a" * 64,
        expected_configuration_fingerprint="b" * 64,
    )
    for field in ("owner_id", "start_at", "observed_microseconds", "actor"):
        with pytest.raises(ValidationError):
            UsagePostingRequest(**base, **{field: "forged"})
    with pytest.raises(ValidationError):
        UsageLedgerHead(node_id="node-a", revision=0, fingerprint="a" * 64)
