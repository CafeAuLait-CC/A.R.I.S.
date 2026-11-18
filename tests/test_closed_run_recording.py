from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from time import sleep
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import (
    ClosedRunRecordingRow,
    UsageDerivation,
    UsageLedgerClaim,
    UsageLedgerEntryRow,
    UsageLedgerOperationRow,
    UsageLedgerState,
)
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_evidence_service import ResidencyRunEvidenceService
from aris.modules.lyra.run_recording_schemas import ClosedRunRecordingRequest
from aris.modules.lyra.run_recording_service import ClosedRunRecordingService
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
    UsageTraceConsumeRequest,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from tests.test_administration import NOW, admin_state
from tests.test_heartbeat import beat, heartbeat
from tests.test_mapping_history import history_state
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger"]
TABLES = (
    ClosedRunRecordingRow,
    UsageDerivation,
    UsageLedgerEntryRow,
    UsageLedgerClaim,
    UsageLedgerOperationRow,
    UsageLedgerState,
    DomainEvent,
    OutboxEntry,
    AuditEntry,
)


@pytest.fixture
def closed(ledger):
    source, original, history = ledger[2]
    sessions, clock = history[1], history[5]
    archive = VegaUsageArchive(sessions, clock=lambda: clock["now"])
    config = UsageTraceConfiguration(
        consumer_id="closed-run-fixture",
        source_ref="closed-run-fixture-rules",
        agent_id="agent-a",
        node_id="node-a",
        initial_after_seq=2,
        continuity=original.continuity,
        formation=original.formation,
    )
    consumer = UsageTraceConsumerService(
        sessions, archive, source.observations, configuration=config, clock=lambda: clock["now"]
    )
    ctx = UsageTraceConsumeContext(request_id="closed-run-fixture")
    first = consumer.consume(
        ctx,
        UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=config.fingerprint,
            expected_revision=0,
            through_seq=7,
        ),
    )
    open_record = consumer.exact(first.id)
    assert beat(ledger[4], heartbeat(seq=8)).status_code == 200
    idle = resource_batch(seq=9, observed_at=NOW + timedelta(seconds=55))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(ledger[4], idle).status_code == 200
    clock["now"] = max(datetime.now(UTC), clock["now"]) + timedelta(seconds=1)
    final = consumer.consume(
        ctx,
        UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=config.fingerprint,
            expected_revision=1,
            through_seq=9,
        ),
    )
    record = consumer.exact(final.id)
    ref = ResidencyRunReference(
        record_id=final.id,
        expected_record_fingerprint=record.fingerprint,
        run_id=record.closed_runs[0].run.id,
        source_cutoff=clock["now"],
    )
    request = ClosedRunRecordingRequest(
        id=uuid4(),
        reference=ref,
        expected_trace_configuration_fingerprint=config.fingerprint,
        expected_recording_configuration_fingerprint=ledger[3].fingerprint,
        identity_revision=original.identity_revision,
    )
    evidence = ResidencyRunEvidenceService(sessions, archive, clock=lambda: clock["now"])
    service = ClosedRunRecordingService(
        sessions, evidence, archive, source, ledger[0], clock=lambda: clock["now"]
    )
    return service, request, ledger, open_record


def counts(state):
    with state[0].sessions() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in TABLES)


def test_closed_proof_original_history_and_ledger_form_one_atomic_replayable_result(closed):
    service, request, ledger, _ = closed
    before = counts(closed)
    result = service.record(POST, request)
    assert not result.duplicate and result.ledger.effect == "recorded"
    assert result.derivation_id == request.derivation_id and not result.quota_usage_available
    assert counts(closed) == tuple(
        a + b for a, b in zip(before, (1, 1, 1, 1, 1, 1, 3, 3, 3), strict=True)
    )
    entry = ledger[0].exact(result.ledger.entries[0])
    assert entry.details.measurement.observed_microseconds == 30000000
    assert entry.details.measurement.source_derivation_id == request.derivation_id
    committed = counts(closed)
    service.ledger.configuration = None
    replay = service.record(POST, request)
    assert replay.duplicate and replay.model_copy(update={"duplicate": False}) == result
    assert counts(closed) == committed
    with service.sessions() as session:
        event_row = session.scalar(
            select(DomainEvent).where(DomainEvent.event_id == str(result.event_id))
        )
        audit = session.scalar(select(AuditEntry).where(AuditEntry.target == str(request.id)))
        assert "linux-user" not in str(event_row.payload) + str(audit.changes)


def test_new_key_same_fixed_run_uses_the_existing_measurement_without_duplicate_time(closed):
    service, request, _, _ = closed
    first = service.record(POST, request)
    second = service.record(POST, request.model_copy(update={"id": uuid4()}))
    assert (
        second.ledger.effect == "already_recorded" and second.ledger.entries == first.ledger.entries
    )
    assert second.derivation_id != first.derivation_id and counts(closed)[:6] == (2, 3, 1, 1, 2, 1)


def test_open_run_or_changed_source_and_configuration_binding_cannot_write(closed):
    service, request, _, opened = closed
    before = counts(closed)
    open_ref = ResidencyRunReference(
        record_id=opened.request.id,
        expected_record_fingerprint=opened.fingerprint,
        run_id=opened.after_residency.open_runs[0].id,
        source_cutoff=request.reference.source_cutoff,
    )
    for changes, code in (
        ({"reference": open_ref}, "USAGE_NEEDS_REVIEW"),
        ({"expected_trace_configuration_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_recording_configuration_fingerprint": "f" * 64}, "CONFLICT"),
    ):
        with pytest.raises(DomainError) as error:
            service.record(POST, request.model_copy(update=changes))
        assert error.value.code == code and counts(closed) == before
    service.ledger.configuration = None
    with pytest.raises(DomainError) as error:
        service.record(POST, request)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(closed) == before


def test_same_key_different_identity_revision_or_cutoff_cannot_relabel_a_receipt(closed):
    service, request, _, _ = closed
    service.record(POST, request)
    before = counts(closed)
    for changes in (
        {"identity_revision": request.identity_revision + 1},
        {
            "reference": request.reference.model_copy(
                update={
                    "source_cutoff": request.reference.source_cutoff - timedelta(microseconds=1)
                }
            )
        },
    ):
        with pytest.raises(DomainError) as error:
            service.record(POST, request.model_copy(update=changes))
        assert error.value.code == "CONFLICT" and counts(closed) == before


def test_missing_closure_witness_is_not_replaced_by_the_valid_busy_endpoints(closed):
    service, request, _, _ = closed
    with service.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 9))
    before = counts(closed)
    with pytest.raises(DomainError):
        service.record(POST, request)
    assert counts(closed) == before


def test_final_source_recheck_failure_rolls_back_derivation_ledger_and_envelope(
    closed, monkeypatch
):
    service, request, _, _ = closed
    verify, calls = service.evidence.verify_in_transaction, []

    def failed(session, reference):
        calls.append(True)
        if len(calls) == 2:
            raise DomainError("CONFLICT", "fixture final proof changed", 409)
        return verify(session, reference)

    monkeypatch.setattr(service.evidence, "verify_in_transaction", failed)
    before = counts(closed)
    with pytest.raises(DomainError, match="fixture final"):
        service.record(POST, request)
    assert len(calls) == 2 and counts(closed) == before


def test_pause_at_final_record_insert_rolls_back_every_composed_write(closed):
    service, request, _, _ = closed
    engine, before = service.sessions.kw["bind"], counts(closed)

    def paused(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO usage_run_recordings"):
            service.ledger.configuration = None

    event.listen(engine, "before_cursor_execute", paused)
    try:
        with pytest.raises(DomainError) as error:
            service.record(POST, request)
    finally:
        event.remove(engine, "before_cursor_execute", paused)
    assert error.value.code == "USAGE_NOT_CONFIGURED" and counts(closed) == before


def test_invalid_public_proof_contract_is_unavailable_with_zero_partial_state(closed, monkeypatch):
    service, request, _, _ = closed
    original = service.evidence.verify(request.reference)
    monkeypatch.setattr(
        service.evidence,
        "verify_in_transaction",
        lambda *args: original.model_copy(update={"fingerprint": "f" * 64}),
    )
    before = counts(closed)
    with pytest.raises(DomainError) as error:
        service.record(POST, request)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(closed) == before


@pytest.mark.parametrize(
    "table", ["usage_run_recordings", "usage_ledger_entries", "usage_derivations"]
)
def test_write_failure_keeps_all_composed_authorities_atomic(closed, table):
    service, request, _, _ = closed
    engine, before = service.sessions.kw["bind"], counts(closed)

    def failed(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table):
            raise RuntimeError("fixture composed write failure")

    event.listen(engine, "before_cursor_execute", failed)
    try:
        with pytest.raises(RuntimeError):
            service.record(POST, request)
    finally:
        event.remove(engine, "before_cursor_execute", failed)
    assert counts(closed) == before
    assert service.record(POST, request).ledger.effect == "recorded"


def test_outer_transaction_can_rollback_a_provisional_closed_run_receipt(closed):
    service, request, _, _ = closed
    before = counts(closed)
    with service.sessions() as session:
        assert not service.record_in_transaction(session, POST, request).duplicate
        session.rollback()
    assert counts(closed) == before


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_acknowledgement_retries_exact_key_without_duplicate_measurement(
    closed, monkeypatch, when
):
    import aris.modules.lyra.run_recording_service as module

    service, request, _, _ = closed
    commit = module.commit_owned_transaction

    def failed(session):
        if when == "after":
            commit(session)
        raise RuntimeError("fixture unknown composed commit")

    monkeypatch.setattr(module, "commit_owned_transaction", failed)
    with pytest.raises(RuntimeError):
        service.record(POST, request)
    monkeypatch.setattr(module, "commit_owned_transaction", commit)
    result = service.record(POST, request)
    assert result.duplicate is (when == "after") and counts(closed)[:6] == (1, 2, 1, 1, 1, 1)


def test_confirmation_history_is_immutable_and_blocks_destructive_downgrade(closed):
    service, request, _, _ = closed
    service.record(POST, request)
    for sql in (
        "UPDATE usage_run_recordings SET run_id='changed'",
        "DELETE FROM usage_run_recordings",
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as session:
            session.execute(text(sql))
    with pytest.raises(RuntimeError, match="usage run recording history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0018")


def test_empty_new_table_migration_roundtrip_preserves_prior_trace_and_derivation(closed):
    service, request, _, opened = closed
    before = service.derivations.exact(closed[2][2][1].id)
    command.downgrade(Config("alembic.ini"), "0018")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    assert service.derivations.exact(before.receipt.id) == before
    assert service.record(POST, request).ledger.effect == "recorded"


def test_request_cannot_supply_owner_time_quantity_or_session_index():
    request = ClosedRunRecordingRequest(
        id=uuid4(),
        reference=ResidencyRunReference(
            record_id=uuid4(),
            expected_record_fingerprint="a" * 64,
            run_id="b" * 64,
            source_cutoff=NOW,
        ),
        expected_trace_configuration_fingerprint="c" * 64,
        expected_recording_configuration_fingerprint="d" * 64,
        identity_revision=1,
    )
    for field, value in (("owner_id", uuid4()), ("microseconds", 1), ("session_index", 0)):
        with pytest.raises(ValidationError):
            ClosedRunRecordingRequest.model_validate({**request.model_dump(), field: value})


def test_unknown_historical_identity_and_pre_recording_window_remain_unrecorded(closed):
    service, request, _, _ = closed
    before = counts(closed)
    assert request.identity_revision > 1
    with pytest.raises(DomainError) as error:
        service.record(
            POST, request.model_copy(update={"identity_revision": request.identity_revision - 1})
        )
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(closed) == before
    service.ledger.configuration = service.ledger.configuration.model_copy(
        update={"recording_start_at": NOW + timedelta(seconds=20)}
    )
    changed = request.model_copy(
        update={
            "expected_recording_configuration_fingerprint": service.ledger.configuration.fingerprint
        }
    )
    with pytest.raises(DomainError) as error:
        service.record(POST, changed)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(closed) == before


def test_postgresql_same_key_concurrency_produces_one_complete_operation(closed):
    service, request, _, _ = closed
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL composed operation arbitration")
    start = Barrier(2)

    def run():
        start.wait(timeout=5)
        return service.record(POST, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(item.duplicate for item in results) == 1 and counts(closed)[:6] == (1, 2, 1, 1, 1, 1)


def test_postgresql_saved_receipt_replay_is_read_only_and_preserves_owner_budgets(closed):
    service, request, _, _ = closed
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL historical replay budget")
    first = service.record(POST, request)
    service.ledger.configuration = None
    before = counts(closed)
    with service.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='3s'"))
        session.execute(text("SET LOCAL statement_timeout='700ms'"))
        replay = service.record_in_transaction(session, POST, request)
        assert replay.duplicate and replay.model_copy(update={"duplicate": False}) == first
        assert session.scalar(text("SHOW transaction_timeout")) == "3s"
        assert session.scalar(text("SHOW statement_timeout")) == "700ms"
    assert counts(closed) == before


def test_postgresql_new_keys_same_run_share_one_measurement_and_truncate_is_refused(closed):
    service, request, _, _ = closed
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL composed measurement concurrency")
    start = Barrier(2)

    def run(item):
        start.wait(timeout=5)
        return service.record(POST, item)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (request, request.model_copy(update={"id": uuid4()}))))
    assert {item.ledger.effect for item in results} == {"recorded", "already_recorded"}
    assert counts(closed)[:6] == (2, 3, 1, 1, 2, 1)
    with pytest.raises(DBAPIError), service.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_run_recordings"))


def test_postgresql_composed_recording_tolerates_remote_round_trips_with_original_budget(closed):
    service, request, _, _ = closed
    engine = service.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL remote round-trip composition")

    def latency(connection, cursor, statement, parameters, context, many):
        sleep(0.055)

    event.listen(engine, "before_cursor_execute", latency)
    try:
        result = service.record(POST, request)
    finally:
        event.remove(engine, "before_cursor_execute", latency)
    assert result.ledger.effect == "recorded" and counts(closed)[:6] == (1, 2, 1, 1, 1, 1)
