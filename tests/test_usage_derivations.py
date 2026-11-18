from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.derivation_repo import UsageDerivationRepository
from aris.modules.lyra.derivation_schemas import (
    UsageDerivationArtifact,
    UsageDerivationContext,
    UsageDerivationRequest,
    canonical_derivation,
)
from aris.modules.lyra.derivation_service import UsageDerivationService
from aris.modules.lyra.models import UsageDerivation
from aris.modules.lyra.reservation_links import ResidencyReservationService
from aris.modules.lyra.residency_attribution import ResidencyAttributionService
from aris.modules.lyra.residency_sessions import ResidencyFormationRule
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.nova.usage_link_service import ReservationUsageLinkService
from aris.modules.vega.models import Node
from aris.modules.vega.usage_schemas import UsageObservationQuery
from aris.modules.vega.usage_service import VegaUsageObservations
from tests.test_administration import NOW, admin_state
from tests.test_historical_usage_facts import owner, ownership
from tests.test_mapping_history import assign, history_state
from tests.test_resource_telemetry import put, resource_batch

__all__ = ["admin_state", "history_state"]
CONTEXT = UsageDerivationContext(request_id="fixture-usage-derivation")


@pytest.fixture
def derivations(history_state, client):
    _, sessions, engine, _, _, clock, identities = history_state
    assigned = assign(history_state, 0)
    with sessions.begin() as session:
        session.get(Node, "node-a").agent_id = "agent-a"
    queries = []
    for index in range(3):
        payload = ownership(
            resource_batch(seq=3 + index * 2, observed_at=NOW + timedelta(seconds=10 + 15 * index)),
            [owner(username="linux-user")],
        )
        assert put(client, payload).status_code == 200
        queries.append(
            UsageObservationQuery(
                agent_id=payload["agent_id"],
                node_id=payload["node_id"],
                message_id=payload["message_id"],
                expected_seq=payload["seq"],
            )
        )
    cutoff = max(datetime.now(UTC), NOW + timedelta(seconds=200))
    clock["now"] = cutoff + timedelta(seconds=1)
    observations = VegaUsageObservations(sessions)
    attribution = ResidencyAttributionService(sessions, identities)
    reservations = ResidencyReservationService(
        sessions, ReservationUsageLinkService(sessions, clock=lambda: clock["now"])
    )
    service = UsageDerivationService(
        sessions, observations, attribution, reservations, clock=lambda: clock["now"]
    )
    request = UsageDerivationRequest(
        id=uuid4(),
        sources=tuple(queries),
        as_of=cutoff,
        identity_revision=assigned.revision,
        continuity=UsageContinuityRule(max_gap_seconds=45),
        formation=ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30),
    )
    return service, request, history_state


def counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (UsageDerivation, DomainEvent, OutboxEntry, AuditEntry)
        )


def test_original_vega_sources_and_history_are_recomputed_and_saved_atomically(derivations):
    service, request, state = derivations
    before = counts(state[1])
    receipt = service.record(CONTEXT, request)
    assert not receipt.duplicate and receipt.candidate_only and not receipt.formal_usage_available
    assert receipt.source_count == 3 and counts(state[1]) == tuple(value + 1 for value in before)
    stored = service.exact(request.id)
    assert stored.receipt.duplicate and stored.receipt.created_at == receipt.created_at
    assert (
        stored.artifact.analysis.request.attribution.attributions[0].identity.status == "resolved"
    )
    assert stored.artifact.analysis.links[0].state == "not_provided"
    assert (
        stored.artifact.analysis.request.attribution.residency.sessions[0].observed_microseconds
        == 30000000
    )
    assert tuple(source.query for source in stored.artifact.sources) == request.sources


def test_exact_replay_canonical_reordering_and_duplicates_preserve_original_receipt(derivations):
    service, request, state = derivations
    first = service.record(CONTEXT, request)
    before = counts(state[1])
    reordered = request.model_copy(
        update={"sources": tuple(reversed(request.sources)) + (request.sources[0],)}
    )
    state[5]["now"] += timedelta(hours=1)
    second = service.record(UsageDerivationContext(request_id="another-fixture-worker"), reordered)
    assert second.duplicate and second.model_copy(update={"duplicate": False}) == first
    assert counts(state[1]) == before


def test_same_id_different_rules_or_cutoff_conflicts_without_extra_history(derivations):
    service, request, state = derivations
    service.record(CONTEXT, request)
    before = counts(state[1])
    for change in (
        {"as_of": request.as_of + timedelta(microseconds=1)},
        {"formation": ResidencyFormationRule(minimum_intervals=1, minimum_duration_seconds=0)},
    ):
        with pytest.raises(DomainError) as error:
            service.record(CONTEXT, request.model_copy(update=change))
        assert error.value.code == "CONFLICT" and counts(state[1]) == before


def test_new_recomputation_id_appends_separate_evidence_without_overwriting_old(derivations):
    service, request, state = derivations
    first = service.record(CONTEXT, request)
    other = request.model_copy(update={"id": uuid4()})
    second = service.record(CONTEXT, other)
    assert second.id != first.id and not second.duplicate
    assert service.exact(first.id).receipt.result_fingerprint == first.result_fingerprint
    assert counts(state[1])[0] == 2 and not second.formal_usage_available


def test_unavailable_original_message_does_not_skip_the_point_or_store_partial_success(derivations):
    service, request, state = derivations
    missing = request.sources[1].model_copy(update={"message_id": uuid4()})
    bad = request.model_copy(update={"sources": (request.sources[0], missing, request.sources[2])})
    before = counts(state[1])
    with pytest.raises(DomainError):
        service.record(CONTEXT, bad)
    assert counts(state[1]) == before


def test_future_cutoff_and_changed_source_binding_fail_before_writes(derivations):
    service, request, state = derivations
    before = counts(state[1])
    with pytest.raises(DomainError) as error:
        service.record(
            CONTEXT, request.model_copy(update={"as_of": state[5]["now"] + timedelta(seconds=1)})
        )
    assert error.value.code == "VALIDATION_ERROR"
    wrong = request.sources[0].model_copy(update={"expected_seq": 1})
    with pytest.raises(DomainError):
        service.record(
            CONTEXT, request.model_copy(update={"sources": (wrong, *request.sources[1:])})
        )
    assert counts(state[1]) == before


def test_source_contract_cannot_return_a_different_message(derivations, monkeypatch):
    service, request, state = derivations
    original = service.observations.observations_in_transaction

    def changed(session, query):
        facts = original(session, query)
        return (facts[0].model_copy(update={"message_id": uuid4()}), *facts[1:])

    monkeypatch.setattr(service.observations, "observations_in_transaction", changed)
    before = counts(state[1])
    with pytest.raises(DomainError, match="binding differs"):
        service.record(CONTEXT, request)
    assert counts(state[1]) == before


def test_source_batch_cannot_silently_omit_an_original_reference(derivations, monkeypatch):
    service, request, state = derivations
    original = service.observations.observations_in_transaction
    monkeypatch.setattr(
        service.observations,
        "observations_in_transaction",
        lambda session, query: original(session, query)[:-1],
    )
    before = counts(state[1])
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, request)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(state[1]) == before


def test_final_recomputation_rejects_changed_facts_instead_of_returning_partial_result(
    derivations, monkeypatch
):
    service, request, state = derivations
    original = service.observations.observations_in_transaction
    calls = []

    def changed(session, query):
        facts = original(session, query)
        calls.append(True)
        return (
            facts
            if len(calls) == 1
            else (facts[0].model_copy(update={"source_fingerprint": "f" * 64}), *facts[1:])
        )

    monkeypatch.setattr(service.observations, "observations_in_transaction", changed)
    before = counts(state[1])
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, request)
    assert error.value.code == "CONFLICT" and counts(state[1]) == before


@pytest.mark.parametrize("table", ["usage_derivations", "event_outbox", "audit_logs"])
def test_failure_rolls_back_derivation_event_outbox_and_audit_together(derivations, table):
    service, request, state = derivations
    before = counts(state[1])

    def fail(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO " + table in statement:
            raise RuntimeError("fixture derivation interruption")

    event.listen(state[2], "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            service.record(CONTEXT, request)
    finally:
        event.remove(state[2], "before_cursor_execute", fail)
    assert counts(state[1]) == before
    assert not service.record(CONTEXT, request).duplicate


def test_owner_transaction_rollback_preserves_no_accepted_candidate(derivations):
    service, request, state = derivations
    before = counts(state[1])
    with state[1]() as session:
        receipt = service.record_in_transaction(session, CONTEXT, request)
        assert not receipt.duplicate
        session.rollback()
    assert counts(state[1]) == before


@pytest.mark.parametrize(
    "statement", ["UPDATE usage_derivations SET node_id='changed'", "DELETE FROM usage_derivations"]
)
def test_stored_candidate_evidence_is_immutable_at_the_database_layer(derivations, statement):
    service, request, state = derivations
    service.record(CONTEXT, request)
    with pytest.raises(DBAPIError):
        with state[1].begin() as session:
            session.execute(text(statement))
    assert service.exact(request.id).artifact.request == request


def test_empty_migration_roundtrip_and_nonempty_downgrade_guard(derivations):
    service, request, state = derivations
    config = Config("alembic.ini")
    command.downgrade(config, "0015")
    assert "usage_derivations" not in inspect(state[2]).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    service.record(CONTEXT, request)
    before = counts(state[1])
    with pytest.raises(RuntimeError, match="usage derivation history prevents downgrade"):
        command.downgrade(config, "0015")
    assert counts(state[1]) == before


def test_dirty_tracked_record_does_not_pollute_scalar_exact_read(derivations):
    service, request, state = derivations
    service.record(CONTEXT, request)
    with state[1]() as session:
        row = session.get(UsageDerivation, str(request.id))
        row.canonical_result = "{}"
        with session.no_autoflush:
            stored = UsageDerivationRepository(session).get(request.id)
        assert stored.receipt.duplicate and stored.artifact.request == request
        session.rollback()


def test_commit_interruption_does_not_report_success_or_leak_pending_pooled_work(derivations):
    service, request, state = derivations
    before = counts(state[1])

    def interrupted(connection):
        raise RuntimeError("fixture interrupted commit")

    event.listen(state[2], "commit", interrupted)
    try:
        with pytest.raises(RuntimeError):
            service.record(CONTEXT, request)
    finally:
        event.remove(state[2], "commit", interrupted)
    assert counts(state[1]) == before
    assert not service.record(CONTEXT, request).duplicate


def test_artifact_references_cannot_be_relabelled_or_silently_dropped(derivations):
    service, request, _ = derivations
    service.record(CONTEXT, request)
    body = service.exact(request.id).artifact.model_dump(mode="json")
    body["sources"][0]["fingerprint"] = "f" * 64
    with pytest.raises(ValidationError):
        UsageDerivationArtifact.model_validate(body)
    body["sources"] = body["sources"][1:]
    with pytest.raises(ValidationError):
        UsageDerivationArtifact.model_validate(body)


def test_malformed_stored_canonical_payload_is_unavailable_without_echoing_it(derivations):
    service, request, state = derivations
    service.record(CONTEXT, request)
    with state[1]() as session:
        row = dict(session.execute(select(*UsageDerivation.__table__.c)).mappings().one())
    row["canonical_result"] = '{"private-fixture":"do-not-echo"}'
    with pytest.raises(DomainError) as error:
        UsageDerivationRepository._stored(row, duplicate=True)
    assert "do-not-echo" not in str(error.value) and error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_conflicting_source_ids_sequences_and_bindings_are_rejected(derivations):
    _, request, _ = derivations
    for other in (
        request.sources[0].model_copy(update={"expected_seq": 9}),
        request.sources[0].model_copy(update={"node_id": "node-b"}),
        request.sources[1].model_copy(update={"expected_seq": request.sources[0].expected_seq}),
    ):
        with pytest.raises(ValidationError):
            UsageDerivationRequest.model_validate(
                request.model_copy(update={"sources": (*request.sources, other)}).model_dump(
                    mode="json"
                )
            )


def test_input_byte_budget_is_checked_before_loading_any_unavailable_sources(derivations):
    service, request, state = derivations
    sources = tuple(
        UsageObservationQuery(
            agent_id="a" * 128, node_id="n" * 128, message_id=uuid4(), expected_seq=i + 1
        )
        for i in range(1024)
    )
    oversized = request.model_copy(update={"sources": sources})
    assert len(canonical_derivation(oversized).encode()) > 262144
    before = counts(state[1])
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, oversized)
    assert error.value.code == "VALIDATION_ERROR" and counts(state[1]) == before


def test_result_byte_budget_refuses_partial_storage_and_accepts_exact_boundary(
    derivations, monkeypatch
):
    import aris.modules.lyra.derivation_service as module

    service, request, state = derivations
    with state[1]() as session:
        size = len(canonical_derivation(service._compute(session, request)).encode())
    before = counts(state[1])
    monkeypatch.setattr(module, "MAX_DERIVATION_RESULT_BYTES", size - 1)
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, request)
    assert error.value.code == "VALIDATION_ERROR" and counts(state[1]) == before
    monkeypatch.setattr(module, "MAX_DERIVATION_RESULT_BYTES", size)
    assert not service.record(CONTEXT, request).duplicate


def test_clock_rollback_before_cutoff_cannot_create_a_new_record(derivations):
    service, request, state = derivations
    times = iter((request.as_of + timedelta(seconds=1), request.as_of - timedelta(seconds=1)))
    service.clock = lambda: next(times)
    before = counts(state[1])
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, request)
    assert error.value.code == "CONFLICT" and counts(state[1]) == before


def test_invalid_dependency_payload_is_unavailable_without_echoing_owner_data(
    derivations, monkeypatch
):
    service, request, state = derivations
    original = service.observations.observations_in_transaction

    def malformed(session, query):
        facts = original(session, query)
        return (
            facts[0].model_copy(update={"compute_quality": "private-fixture-must-not-be-echoed"}),
            *facts[1:],
        )

    monkeypatch.setattr(service.observations, "observations_in_transaction", malformed)
    with pytest.raises(DomainError) as error:
        service.record(CONTEXT, request)
    assert (
        "private-fixture" not in str(error.value) and error.value.code == "DEPENDENCY_UNAVAILABLE"
    )


def test_postgresql_competing_workers_create_one_record_and_one_success_event(derivations):
    service, request, state = derivations
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL derivation record ID lock and idempotency")
    before = counts(state[1])
    barrier = Barrier(2)

    def record(index):
        barrier.wait(timeout=10)
        return service.record(UsageDerivationContext(request_id=f"fixture-worker-{index}"), request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, range(2)))
    assert sum(item.duplicate for item in results) == 1
    assert len({item.event_id for item in results}) == 1
    assert counts(state[1]) == tuple(value + 1 for value in before)


def test_postgresql_truncate_is_refused(derivations):
    service, request, state = derivations
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL derivation TRUNCATE protection")
    service.record(CONTEXT, request)
    with pytest.raises(DBAPIError):
        with state[1].begin() as session:
            session.execute(text("TRUNCATE usage_derivations"))


def test_postgresql_competing_different_input_cannot_reuse_a_record_id(derivations):
    service, request, state = derivations
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL derivation conflicting input arbitration")
    changed = request.model_copy(
        update={
            "formation": ResidencyFormationRule(minimum_intervals=1, minimum_duration_seconds=0)
        }
    )
    barrier = Barrier(2)

    def record(value):
        barrier.wait(timeout=10)
        try:
            return service.record(CONTEXT, value)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, (request, changed)))
    assert results.count("CONFLICT") == 1 and counts(state[1])[0] == 1


def test_postgresql_busy_record_lock_times_out_then_retries_without_partial_state(derivations):
    service, request, state = derivations
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL derivation bounded advisory-lock wait")
    before = counts(state[1])
    with state[1]() as held:
        UsageDerivationRepository(held).lock(request.id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.record, CONTEXT, request)
            with pytest.raises(DomainError) as error:
                future.result(timeout=5)
        assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(state[1]) == before
        held.rollback()
    assert not service.record(CONTEXT, request).duplicate


def test_postgresql_exact_evidence_read_works_in_read_only_transaction(derivations):
    service, request, state = derivations
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL derivation exact read READ ONLY contract")
    service.record(CONTEXT, request)
    before = counts(state[1])
    with state[1].begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        stored = UsageDerivationRepository(session).get(request.id)
        assert stored.receipt.id == request.id
    assert counts(state[1]) == before
