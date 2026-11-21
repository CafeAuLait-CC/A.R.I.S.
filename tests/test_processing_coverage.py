from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.history_service import NodeUserHistoryService
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.coverage_schemas import (
    ProcessingCoverageConfiguration,
    ProcessingCoverageRequest,
)
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.models import UsageCoverageManifestRow, UsageTraceAttestationRow
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.trace_attestation_schemas import TraceAttestationRequest
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.vega.models import AgentMessage
from tests.test_administration import admin_state
from tests.test_closed_run_recording import closed
from tests.test_historical_usage_facts import owner, ownership
from tests.test_mapping_history import history_state
from tests.test_partition_recording import partition_recording
from tests.test_resource_telemetry import put, resource_batch
from tests.test_trace_consumer import CONTEXT, consumer, request
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, ledger
from tests.test_usage_reversals import adjustments, confirm, pending

__all__ = [
    "admin_state",
    "history_state",
    "derivations",
    "ledger",
    "closed",
    "consumer",
    "partition_recording",
    "adjustments",
]


def head(service):
    with service.sessions() as session:
        return UsageLedgerRepository(session).head(service.configuration.trace.node_id)


def capture_request(service, attestation, start, end):
    return ProcessingCoverageRequest(
        id=uuid4(),
        attestation_id=attestation.request.id,
        expected_attestation_fingerprint=attestation.fingerprint,
        expected_configuration_fingerprint=service.configuration.fingerprint,
        start_at=start,
        end_at=end,
        ledger=head(service),
    )


def attest(service, trace, record):
    return service.record(
        CONTEXT,
        TraceAttestationRequest(
            id=uuid4(),
            record_id=record.request.id,
            expected_record_fingerprint=record.fingerprint,
            source_cutoff=trace._now(),
        ),
    ).attestation


@pytest.fixture
def coverage(closed):
    writer, incoming, ledger_state, opened = closed
    config = opened.before.configuration
    trace = UsageTraceConsumerService(
        writer.sessions,
        writer.archive,
        writer.derivations.observations,
        configuration=config,
        clock=writer.clock,
    )
    proofs = TraceAttestationService(writer.sessions, trace, clock=writer.clock)
    first = attest(proofs, trace, opened)
    last = attest(proofs, trace, trace.exact(incoming.reference.record_id))
    configuration = ProcessingCoverageConfiguration(
        trace=config,
        recording=ledger_state[3],
        gpu_uuids=("GPU-a",),
    )
    identities = ledger_state[2][2][6]
    service = ProcessingCoverageService(
        writer.sessions,
        proofs,
        writer.archive,
        writer.derivations.observations,
        identities,
        configuration=configuration,
        clock=writer.clock,
    )
    run = opened.after_residency.open_runs[0]
    return SimpleNamespace(
        service=service,
        writer=writer,
        incoming=incoming,
        ledger=ledger_state,
        first=first,
        last=last,
        run=run,
    )


def snapshot(service):
    with service.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (UsageCoverageManifestRow, DomainEvent, OutboxEntry, AuditEntry)
        )


def incoming(state, **changes):
    value = capture_request(
        state.service,
        state.first,
        state.run.first.compute_observed_at,
        state.run.last.compute_observed_at,
    )
    return value.model_copy(update=changes)


@pytest.mark.parametrize(
    "method", ["strict_sampled_residency_edges", "indexed_sampled_residency_edges"]
)
def test_closed_processing_changes_pending_to_known_without_rewriting_the_old_manifest(
    coverage, method
):
    state = coverage
    service = state.service
    service.configuration = service.configuration.model_copy(update={"method": method})
    request = incoming(state)
    before = snapshot(service)
    pending = service.record(CONTEXT, request)
    assert not pending.duplicate
    analysis = pending.manifest.analysis
    assert not analysis.complete_within_source_window
    assert analysis.known_recorded_microseconds == analysis.uncovered_gpu_microseconds == 0
    assert analysis.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in analysis.cells} == {"RESIDENCY_NOT_RECORDED"}
    recorded = state.writer.record(POST, state.incoming)
    current = service.record(CONTEXT, incoming(state)).manifest.analysis
    assert (
        current.complete_within_source_window and current.known_recorded_microseconds == 30_000_000
    )
    assert current.unknown_gpu_microseconds == current.uncovered_gpu_microseconds == 0
    assert {cell.state for cell in current.cells} == {"recorded"}
    assert current.recordings[0].entry_id == recorded.ledger.entries[0]
    assert not current.quota_usage_available and current.total_usage_microseconds is None
    assert not current.full_source_prefix_assessed
    assert service.exact(request.id) == pending.manifest
    assert service.reverify(request.id) == pending.manifest
    assert service.record(CONTEXT, request).duplicate
    assert snapshot(service) == tuple(a + b for a, b in zip(before, (2, 5, 5, 5), strict=True))


@pytest.mark.parametrize(
    "method", ["strict_sampled_residency_edges", "indexed_sampled_residency_edges"]
)
def test_an_ordinary_open_run_measurement_needs_a_committed_closing_receipt(coverage, method):
    state = coverage
    state.service.configuration = state.service.configuration.model_copy(update={"method": method})
    measured = state.ledger[0].post(POST, state.ledger[1])
    if method == "indexed_sampled_residency_edges":
        from aris.modules.lyra.ledger_segment_schemas import ledger_read_configuration
        from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
        from tests.test_ledger_segments import measurement

        first = state.ledger[0].exact(measured.entries[0])
        segments = LedgerSegmentService(
            state.service.sessions,
            configuration=ledger_read_configuration(first.node_id),
            clock=state.service.clock,
        )
        # An eventual alias may point to a committed head outside this source window.
        measurement(segments, first, measured.head, state.service._now())
    pending = state.service.record(CONTEXT, incoming(state)).manifest.analysis
    assert pending.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in pending.cells} == {"CLOSED_RECORDING_NOT_VERIFIED"}
    alias = state.writer.record(POST, state.incoming)
    assert alias.ledger.effect == "already_recorded" and alias.ledger.entries == measured.entries
    result = state.service.record(CONTEXT, incoming(state)).manifest.analysis
    assert result.complete_within_source_window
    assert result.recordings[0].entry_id == measured.entries[0]
    assert result.recordings[0].id == state.incoming.id
    assert state.service.reverify(result.request.id).analysis == result


def test_indexed_local_partition_ignores_unrelated_lifetime_boundaries_and_reverifies(coverage):
    from aris.modules.lyra.ledger_segment_schemas import ledger_read_configuration
    from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
    from aris.modules.lyra.window_schemas import UsageWindowQuery
    from aris.modules.lyra.window_snapshot_schemas import (
        CaptureUsageWindow,
        WindowManifestSelection,
    )
    from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
    from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
    from tests.test_ledger_segments import CONTEXT as INDEX_CONTEXT
    from tests.test_ledger_segments import measurement
    from tests.test_ledger_segments import request as index_request

    state = coverage
    receipt = state.writer.record(POST, state.incoming)
    first = state.ledger[0].exact(receipt.ledger.entries[0])
    segments = LedgerSegmentService(
        state.service.sessions,
        configuration=ledger_read_configuration(first.node_id),
        clock=state.service.clock,
    )
    point = receipt.ledger.head
    for _ in range(24):
        _, point = measurement(segments, first, point, state.service._now())
    original = state.service.record(
        CONTEXT,
        capture_request(
            state.service, state.first, state.run.first.compute_observed_at, state.service._now()
        ),
    ).manifest
    state.service.configuration = state.service.configuration.model_copy(
        update={"method": "indexed_sampled_residency_edges"}
    )
    indexed = state.service.record(
        CONTEXT,
        capture_request(
            state.service, state.first, state.run.first.compute_observed_at, state.service._now()
        ),
    ).manifest
    assert (
        original.analysis.known_recorded_microseconds
        == indexed.analysis.known_recorded_microseconds
        == 30_000_000
    )
    assert (
        original.analysis.uncovered_gpu_microseconds == indexed.analysis.uncovered_gpu_microseconds
    )
    assert len(original.analysis.cells) > 40 and len(indexed.analysis.cells) < 6
    # The final report only needs the original source window. Both manifests
    # point to head25, whose final entry is outside that time selection.
    last = state.service.record(
        CONTEXT,
        capture_request(
            state.service,
            state.last,
            state.run.first.compute_observed_at,
            state.run.last.compute_observed_at,
        ),
    ).manifest
    first_window = state.service.record(
        CONTEXT,
        capture_request(
            state.service,
            state.first,
            state.run.first.compute_observed_at,
            state.run.last.compute_observed_at,
        ),
    ).manifest
    source = state.service.archive.head(UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a"))
    windows = UsageWindowSnapshotService(
        state.service.sessions,
        state.service,
        state.service.archive,
        state.service.identities,
        configuration=state.service.configuration,
        clock=state.service.clock,
    )
    saved = windows.capture(
        CONTEXT,
        CaptureUsageWindow(
            id=uuid4(),
            query=UsageWindowQuery(
                node_id="node-a",
                start_at=state.run.first.compute_observed_at,
                end_at=state.run.last.compute_observed_at,
            ),
            expected_configuration_fingerprint=state.service.configuration.fingerprint,
            expected_source_fingerprint=source.fingerprint,
            ledger=point,
            manifests=tuple(
                WindowManifestSelection(id=value.analysis.request.id, fingerprint=value.fingerprint)
                for value in (first_window, last)
            ),
        ),
    ).snapshot
    assert (
        saved.window.coverage == "complete" and saved.window.total_usage_microseconds == 30_000_000
    )
    segments.capture(INDEX_CONTEXT, index_request(segments, point))
    assert state.service.reverify(indexed.analysis.request.id) == indexed
    assert state.service.reverify(original.analysis.request.id) == original
    assert windows.reverify(saved.request.id) == saved


def test_indexed_source_without_positive_edges_does_not_scan_a_ledger_window(consumer, monkeypatch):
    from aris.modules.lyra.ledger_window_service import LedgerWindowReader

    state = consumer
    assert (
        put(
            state.client, resource_batch(seq=1, observed_at=state.base + timedelta(seconds=10))
        ).status_code
        == 200
    )
    result = state.service.consume(CONTEXT, request(state, through_seq=1))
    proofs = TraceAttestationService(state.sessions, state.service, clock=state.now)
    proof = attest(proofs, state.service, state.service.exact(result.id))
    config = ProcessingCoverageConfiguration(
        method="indexed_sampled_residency_edges",
        trace=state.config,
        gpu_uuids=("GPU-a",),
        recording=UsagePostingConfiguration(
            source_ref="one-observation-fixture",
            recording_start_at=state.base,
            timing_fingerprint=residency_fingerprint(
                state.config.continuity, state.config.formation
            ),
        ),
    )
    service = ProcessingCoverageService(
        state.sessions,
        proofs,
        state.archive,
        state.observations,
        NodeUserHistoryService(state.sessions),
        configuration=config,
        clock=state.now,
    )

    def unexpected(*args, **kwargs):
        pytest.fail("No positive source edge must not trigger a whole or time-window ledger scan")

    monkeypatch.setattr(UsageLedgerRepository, "prefix", unexpected)
    monkeypatch.setattr(LedgerWindowReader, "read_in_transaction", unexpected)
    value = service.record(
        CONTEXT,
        capture_request(
            service, proof, state.base + timedelta(seconds=10), state.base + timedelta(seconds=40)
        ),
    ).manifest
    assert value.analysis.uncovered_gpu_microseconds == 30_000_000
    assert (
        value.analysis.known_recorded_microseconds == value.analysis.unknown_gpu_microseconds == 0
    )
    assert {cell.state for cell in value.analysis.cells} == {"uncovered"}
    assert service.reverify(value.analysis.request.id) == value


def test_idle_edges_are_complete_locally_while_unobserved_boundaries_remain_uncovered(consumer):
    state = consumer
    for seq, seconds in enumerate((10, 25, 40), 1):
        value = resource_batch(seq=seq, observed_at=state.base + timedelta(seconds=seconds))
        value["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert put(state.client, value).status_code == 200
    result = state.service.consume(CONTEXT, request(state, through_seq=3))
    proof_service = TraceAttestationService(state.sessions, state.service, clock=state.now)
    proof = attest(proof_service, state.service, state.service.exact(result.id))
    config = ProcessingCoverageConfiguration(
        trace=state.config,
        gpu_uuids=("GPU-a",),
        recording=UsagePostingConfiguration(
            source_ref="idle-fixture",
            recording_start_at=state.base,
            timing_fingerprint=residency_fingerprint(
                state.config.continuity, state.config.formation
            ),
        ),
    )
    service = ProcessingCoverageService(
        state.sessions,
        proof_service,
        state.archive,
        state.observations,
        NodeUserHistoryService(state.sessions),
        configuration=config,
        clock=state.now,
    )
    value = service.record(
        CONTEXT,
        capture_request(
            service, proof, state.base + timedelta(seconds=10), state.base + timedelta(seconds=40)
        ),
    ).manifest
    assert value.analysis.complete_within_source_window
    assert {cell.state for cell in value.analysis.cells} == {"observed_idle"}
    assert value.analysis.known_recorded_microseconds == 0
    assert service.reverify(value.analysis.request.id) == value
    expanded = service.record(
        CONTEXT, capture_request(service, proof, state.base, state.base + timedelta(seconds=50))
    ).manifest.analysis
    assert expanded.uncovered_gpu_microseconds == 20_000_000
    assert not expanded.complete_within_source_window


def test_transition_out_of_residency_is_unknown_rather_than_charged_or_free(coverage):
    state = coverage
    state.writer.record(POST, state.incoming)
    result = state.service.record(
        CONTEXT,
        capture_request(
            state.service,
            state.last,
            state.run.last.compute_observed_at,
            state.run.last.compute_observed_at + timedelta(seconds=15),
        ),
    )
    assert result.manifest.analysis.unknown_gpu_microseconds == 15_000_000
    assert {cell.state for cell in result.manifest.analysis.cells} == {"unknown"}


def test_disabled_capture_does_not_touch_the_database(coverage):
    service = coverage.service
    request = incoming(coverage)
    service.configuration = None

    def reject(*args, **kwargs):
        raise AssertionError("disabled coverage accessed database")

    event.listen(service.sessions.kw["bind"], "before_cursor_execute", reject)
    try:
        with pytest.raises(DomainError, match="not enabled"):
            service.record(CONTEXT, request)
    finally:
        event.remove(service.sessions.kw["bind"], "before_cursor_execute", reject)


def test_wrong_head_configuration_future_window_and_reused_id_do_not_append(coverage):
    service = coverage.service
    request = incoming(coverage)
    saved = service.record(CONTEXT, request)
    before = snapshot(service)
    bad = (
        request.model_copy(update={"end_at": request.end_at + timedelta(seconds=1)}),
        incoming(coverage, expected_configuration_fingerprint="f" * 64),
        incoming(coverage, expected_attestation_fingerprint="f" * 64),
        incoming(coverage, end_at=service._now() + timedelta(seconds=1)),
    )
    for value in bad:
        with pytest.raises(DomainError):
            service.record(CONTEXT, value)
        assert snapshot(service) == before
    coverage.writer.record(POST, coverage.incoming)
    with pytest.raises(DomainError, match="current committed ledger head"):
        service.record(CONTEXT, request.model_copy(update={"id": uuid4()}))
    assert service.exact(request.id) == saved.manifest


def test_missing_original_body_invalidates_reverification_but_not_historical_read(coverage):
    service = coverage.service
    result = service.record(CONTEXT, incoming(coverage)).manifest
    with service.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 5))
    assert service.exact(result.analysis.request.id) == result
    with pytest.raises(DomainError):
        service.reverify(result.analysis.request.id)


def test_failure_at_final_manifest_write_rolls_back_event_outbox_and_audit(coverage):
    service, before = coverage.service, snapshot(coverage.service)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO usage_coverage_manifests"):
            raise RuntimeError("fixture coverage insert failure")

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="fixture coverage"):
            service.record(CONTEXT, incoming(coverage))
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert snapshot(service) == before


def test_manifest_history_is_immutable_and_blocks_downgrade(coverage):
    service = coverage.service
    saved = service.record(CONTEXT, incoming(coverage)).manifest
    for statement in (
        "UPDATE usage_coverage_manifests SET fingerprint='x'",
        "DELETE FROM usage_coverage_manifests",
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as session:
            session.execute(text(statement))
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0024")
    assert service.exact(saved.analysis.request.id) == saved


def test_postgresql_same_operation_is_only_captured_once(coverage):
    service = coverage.service
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL transaction locks")
    request, barrier, before = incoming(coverage), Barrier(2), snapshot(service)

    def capture():
        barrier.wait()
        return service.record(CONTEXT, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capture(), range(2)))
    assert sorted(value.duplicate for value in results) == [False, True]
    assert results[0].manifest == results[1].manifest
    assert snapshot(service) == tuple(value + 1 for value in before)


@pytest.mark.parametrize(
    "scenario", ["cached", "cached_bad_inventory", "rollback", "unmapped", "shared"]
)
def test_cached_quality_clock_and_unresolved_ownership_keep_their_meaning(consumer, scenario):
    state = consumer
    times = (10, 25, 15, 40) if scenario == "rollback" else (10, 25, 40)
    for seq, seconds in enumerate(times, 1):
        value = resource_batch(
            seq=seq,
            observed_at=state.base + timedelta(seconds=seconds),
            compute_age=15 if scenario.startswith("cached") and seq == 2 else 0,
            gpu_quality="error" if scenario == "cached_bad_inventory" and seq == 2 else "ok",
        )
        if scenario in ("unmapped", "shared"):
            users = [owner()]
            if scenario == "shared":
                value["resources"]["compute"].update(reported_processes=2)
                value["resources"]["compute"]["gpus"][0]["reported_processes"] = 2
                users.append(owner(uid=1002, username="other-user"))
            ownership(value, users)
        else:
            value["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert put(state.client, value).status_code == 200
    receipt = state.service.consume(CONTEXT, request(state, through_seq=len(times)))
    proofs = TraceAttestationService(state.sessions, state.service, clock=state.now)
    proof = attest(proofs, state.service, state.service.exact(receipt.id))
    config = ProcessingCoverageConfiguration(
        trace=state.config,
        gpu_uuids=("GPU-a",),
        recording=UsagePostingConfiguration(
            source_ref="uncertainty-fixture",
            recording_start_at=state.base,
            timing_fingerprint=residency_fingerprint(
                state.config.continuity, state.config.formation
            ),
        ),
    )
    service = ProcessingCoverageService(
        state.sessions,
        proofs,
        state.archive,
        state.observations,
        NodeUserHistoryService(state.sessions),
        configuration=config,
        clock=state.now,
    )
    result = service.record(
        CONTEXT,
        capture_request(
            service, proof, state.base + timedelta(seconds=10), state.base + timedelta(seconds=40)
        ),
    ).manifest.analysis
    assert result.complete_within_source_window == (scenario == "cached")
    assert result.unknown_gpu_microseconds == (0 if scenario == "cached" else 30_000_000)
    if scenario == "unmapped":
        assert {cell.reason for cell in result.cells} == {"IDENTITY_UNRESOLVED"}
        assert result.identities[0].identity.reason == "DIRECTORY_UNINITIALIZED"
    assert service.reverify(result.request.id).analysis == result


def test_partition_receipt_covers_only_its_measured_slice_and_uses_the_whole_parent_identity(
    partition_recording,
):
    writer, request, _, _, _ = partition_recording
    posted = writer.record(
        POST,
        request.model_copy(
            update={"partition": request.partition.model_copy(update={"partition_index": 1})}
        ),
    )
    trace = writer.evidence.records
    proofs = writer.evidence.attestations
    with writer.sessions() as session:
        proof_id = session.scalar(
            select(UsageTraceAttestationRow.id).where(
                UsageTraceAttestationRow.consumer_id == trace.configuration.consumer_id,
                UsageTraceAttestationRow.revision == 2,
            )
        )
    proof = proofs.exact(proof_id)
    record = trace.exact(proof.request.record_id)
    run = record.after_residency.open_runs[0]
    config = ProcessingCoverageConfiguration(
        trace=trace.configuration, recording=writer.ledger.configuration, gpu_uuids=("GPU-a",)
    )
    service = ProcessingCoverageService(
        writer.sessions,
        proofs,
        writer.evidence.archive,
        writer.derivations.observations,
        writer.identities,
        configuration=config,
        clock=writer.clock,
    )
    result = service.record(
        CONTEXT,
        capture_request(
            service,
            proof,
            record.before.trace.anchor.reference.compute_observed_at,
            run.last.compute_observed_at,
        ),
    ).manifest.analysis
    assert result.known_recorded_microseconds == 45_000_000
    assert result.unknown_gpu_microseconds == 15_000_000
    assert result.recordings[0].entry_id == posted.ledger.entries[0]
    assert result.identities[0].run.observed_microseconds == 135_000_000
    assert service.reverify(result.request.id).analysis == result


def test_reversal_makes_new_coverage_unknown_but_preserves_the_original_fixed_snapshot(
    coverage, adjustments
):
    state = coverage
    state.writer.record(POST, state.incoming)
    original = state.service.record(CONTEXT, incoming(state)).manifest
    assert original.analysis.complete_within_source_window
    review = pending(adjustments)
    assert confirm(adjustments, review).state == "succeeded"
    current = state.service.record(CONTEXT, incoming(state)).manifest.analysis
    assert current.known_recorded_microseconds == 0
    assert current.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in current.cells} == {"REVERSED_AWAITING_REVIEW"}
    assert state.service.reverify(original.analysis.request.id) == original
    assert state.service.reverify(current.request.id).analysis == current


def test_empty_coverage_migration_roundtrip_and_metadata_match(coverage):
    command.check(Config("alembic.ini"))
    command.downgrade(Config("alembic.ini"), "0024")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    assert (
        coverage.service.record(
            CONTEXT, incoming(coverage)
        ).manifest.analysis.unknown_gpu_microseconds
        == 30_000_000
    )


def test_postgresql_reverification_works_in_a_read_only_transaction(coverage):
    service = coverage.service
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only transactions")
    coverage.writer.record(POST, coverage.incoming)
    result = service.record(CONTEXT, incoming(coverage)).manifest
    before = snapshot(service)
    with service.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert service.reverify_in_transaction(session, result.analysis.request.id) == result
    assert snapshot(service) == before
