from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_inputs import resolve_coverage_identities
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.window_coverage import sampled_usage_window
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from tests.test_administration import admin_state
from tests.test_closed_run_recording import closed
from tests.test_mapping_history import assign, history_state
from tests.test_processing_coverage import attest, capture_request, coverage, incoming
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
    "coverage",
    "adjustments",
]


def query(state, **changes):
    return UsageWindowQuery(
        node_id="node-a",
        start_at=state.run.first.compute_observed_at,
        end_at=state.run.last.compute_observed_at,
        **changes,
    )


def full_sources(state):
    state.writer.record(POST, state.incoming)
    first = state.service.record(CONTEXT, incoming(state)).manifest
    last = state.service.record(
        CONTEXT,
        capture_request(
            state.service,
            state.last,
            first.analysis.request.start_at,
            first.analysis.request.end_at,
        ),
    ).manifest
    return first, last


def combine(service, manifests, window, *, only_identities=None):
    now = service._now()
    parents = {}
    for value in manifests:
        for cell in value.analysis.cells:
            if cell.state == "recorded":
                parent = value.analysis.identities[cell.identity_index].run
                parents[parent.id] = parent
    with service.sessions() as session:
        repo = UsageLedgerRepository(session)
        head = repo.head(window.node_id)
        entries = repo.prefix(head)
        ceiling = service.archive.head_in_transaction(
            session,
            UsageArchiveHeadQuery(
                agent_id=service.configuration.trace.agent_id, node_id=window.node_id
            ),
        )
        now = service._now()
        identities = resolve_coverage_identities(
            session, service.identities, tuple(parents[key] for key in sorted(parents)), now
        )
    if only_identities is not None:
        identities = only_identities
    revision = identities[0].identity.directory_revision if identities else None
    return sampled_usage_window(
        window,
        service.configuration,
        ceiling,
        head,
        entries,
        manifests,
        identities,
        identity_revision=revision,
        read_at=now,
    )


def test_complete_source_prefix_and_closed_measurement_make_the_sampled_total_available(coverage):
    state = coverage
    manifests = full_sources(state)
    result = combine(state.service, manifests, query(state))
    assert result.source_prefix_complete and result.coverage == "complete"
    assert (
        result.recorded_microseconds
        == result.verified_usage_microseconds
        == result.total_usage_microseconds
        == 30_000_000
    )
    assert result.quota_usage_available and result.covered_gpu_microseconds == 30_000_000
    assert result.unknown_gpu_microseconds == result.uncovered_gpu_microseconds == 0
    assert len(result.cells) == 1 and result.cells[0].state == "recorded"
    assert combine(state.service, tuple(reversed(manifests)), query(state)) == result
    clipped = query(state).model_copy(
        update={
            "start_at": result.query.start_at + timedelta(seconds=5),
            "end_at": result.query.end_at - timedelta(seconds=5),
        }
    )
    assert combine(state.service, manifests, clipped).total_usage_microseconds == 20_000_000
    other = combine(state.service, manifests, query(state, owner_id=uuid4()))
    assert other.coverage == "complete" and other.total_usage_microseconds == 0
    assert other.covered_gpu_microseconds == 30_000_000


def test_omitting_a_later_source_segment_cannot_hide_its_possible_effect_on_the_window(coverage):
    state = coverage
    first, _ = full_sources(state)
    result = combine(state.service, (first,), query(state))
    assert not result.source_prefix_complete and result.coverage == "unknown"
    assert result.total_usage_microseconds is None and not result.quota_usage_available
    assert result.recorded_microseconds == 30_000_000 and result.verified_usage_microseconds == 0
    assert result.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in result.cells} == {"SOURCE_PREFIX_INCOMPLETE"}


def test_local_window_edges_can_be_filled_but_open_or_unprocessed_usage_cannot(coverage):
    state = coverage
    pending_first = state.service.record(CONTEXT, incoming(state)).manifest
    last = state.service.record(
        CONTEXT,
        capture_request(
            state.service,
            state.last,
            pending_first.analysis.request.start_at,
            pending_first.analysis.request.end_at,
        ),
    ).manifest
    result = combine(state.service, (pending_first, last), query(state))
    assert result.source_prefix_complete and result.coverage == "unknown"
    assert result.unknown_gpu_microseconds == 30_000_000 and result.total_usage_microseconds is None
    state.writer.record(POST, state.incoming)
    result = combine(state.service, (pending_first, last), query(state))
    assert result.recorded_microseconds == 30_000_000 and result.total_usage_microseconds is None
    latest_first = state.service.record(CONTEXT, incoming(state)).manifest
    result = combine(state.service, (latest_first, last), query(state))
    assert result.total_usage_microseconds == 30_000_000


def test_overlapping_versions_for_one_source_must_be_selected_explicitly(coverage):
    state = coverage
    first, last = full_sources(state)
    repeated = state.service.record(CONTEXT, incoming(state)).manifest
    with pytest.raises(DomainError, match="nonoverlapping"):
        combine(state.service, (first, repeated, last), query(state))
    with pytest.raises(DomainError, match="scope"):
        combine(state.service, (first, first, last), query(state))


def test_adjacent_time_shards_of_the_same_source_compose_without_double_counting(coverage):
    state = coverage
    state.writer.record(POST, state.incoming)
    window = query(state)
    middle = window.start_at + timedelta(seconds=15)
    first = state.service.record(CONTEXT, incoming(state, end_at=middle)).manifest
    second = state.service.record(CONTEXT, incoming(state, start_at=middle)).manifest
    last = state.service.record(
        CONTEXT, capture_request(state.service, state.last, window.start_at, window.end_at)
    ).manifest
    result = combine(state.service, (first, second, last), window)
    assert result.total_usage_microseconds == 30_000_000 and len(result.cells) == 1


def test_current_reversal_invalidates_old_positive_coverage_without_erasing_recorded_history(
    coverage, adjustments
):
    state = coverage
    manifests = full_sources(state)
    original = combine(state.service, manifests, query(state))
    assert original.total_usage_microseconds == 30_000_000
    assert confirm(adjustments, pending(adjustments)).state == "succeeded"
    current = combine(state.service, manifests, query(state))
    assert current.source_prefix_complete and current.total_usage_microseconds is None
    assert current.recorded_microseconds == current.verified_usage_microseconds == 0
    assert current.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in current.cells} == {"REVERSED_AWAITING_REVIEW"}
    assert original.total_usage_microseconds == 30_000_000


def test_later_known_mapping_change_requires_reassessment_of_the_whole_closed_parent(coverage):
    state = coverage
    manifests = full_sources(state)
    assert combine(state.service, manifests, query(state)).total_usage_microseconds == 30_000_000
    history = state.ledger[2][2]
    now = history[5]["now"]
    assign(history, 25, user=1, expected=history[4][0])
    history[5]["now"] = now + timedelta(seconds=1)
    current = combine(state.service, manifests, query(state))
    assert current.source_prefix_complete and current.total_usage_microseconds is None
    assert current.recorded_microseconds == 30_000_000
    assert current.unknown_gpu_microseconds == 30_000_000
    assert {cell.reason for cell in current.cells} == {"IDENTITY_UNRESOLVED"}


@pytest.mark.parametrize("scenario", ["idle", "rollback", "bad_cached_inventory"])
def test_all_source_diagnostics_apply_even_when_their_selected_time_shard_is_outside_the_window(
    consumer, scenario
):
    state = consumer
    times = (
        (10, 25, 40) if scenario == "idle" else (10, 25, 40, 20 if scenario == "rollback" else 55)
    )
    for seq, seconds in enumerate(times, 1):
        value = resource_batch(
            seq=seq,
            observed_at=state.base + timedelta(seconds=seconds),
            compute_age=15 if scenario == "bad_cached_inventory" and seq == 4 else 0,
            gpu_quality="error" if scenario == "bad_cached_inventory" and seq == 4 else "ok",
        )
        value["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert put(state.client, value).status_code == 200
    proofs = TraceAttestationService(state.sessions, state.service, clock=state.now)
    config = ProcessingCoverageConfiguration(
        trace=state.config,
        gpu_uuids=("GPU-a",),
        recording=UsagePostingConfiguration(
            source_ref="window-idle-fixture",
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
    first = state.service.consume(CONTEXT, request(state, through_seq=3))
    proof = attest(proofs, state.service, state.service.exact(first.id))
    start, end = state.base + timedelta(seconds=10), state.base + timedelta(seconds=40)
    manifests = [service.record(CONTEXT, capture_request(service, proof, start, end)).manifest]
    if scenario != "idle":
        last = state.service.consume(CONTEXT, request(state, through_seq=4))
        proof = attest(proofs, state.service, state.service.exact(last.id))
        manifests.append(
            service.record(
                CONTEXT,
                capture_request(
                    service, proof, end + timedelta(seconds=30), end + timedelta(seconds=40)
                ),
            ).manifest
        )
    result = combine(
        service, tuple(manifests), UsageWindowQuery(node_id="node-a", start_at=start, end_at=end)
    )
    assert result.source_prefix_complete
    assert result.quota_usage_available == (scenario == "idle")
    assert result.total_usage_microseconds == (0 if scenario == "idle" else None)
    expected_unknown = {"idle": 0, "rollback": 30_000_000, "bad_cached_inventory": 15_000_000}
    assert result.unknown_gpu_microseconds == expected_unknown[scenario]
    assert len(result.cells) <= 2


def test_query_rejects_nonexistent_calendar_zone_and_tampered_complete_totals(coverage):
    state = coverage
    with pytest.raises(ValidationError):
        query(state, timezone="Invalid/CalendarZone")
    result = combine(state.service, full_sources(state), query(state))
    for field, value in (
        ("total_usage_microseconds", 0),
        ("recorded_microseconds", 0),
        ("unknown_gpu_microseconds", 1),
    ):
        with pytest.raises(ValidationError):
            type(result).model_validate({**result.model_dump(mode="json"), field: value})


def test_unobserved_window_edges_are_partial_and_do_not_extrapolate_the_last_sample(coverage):
    state = coverage
    manifests = full_sources(state)
    window = query(state)
    expanded = window.model_copy(
        update={
            "start_at": window.start_at - timedelta(seconds=5),
            "end_at": window.end_at + timedelta(seconds=5),
        }
    )
    result = combine(state.service, manifests, expanded)
    assert result.source_prefix_complete and result.coverage == "partial"
    assert result.total_usage_microseconds is None
    assert result.covered_gpu_microseconds == 30_000_000
    assert result.uncovered_gpu_microseconds == 10_000_000


def test_gpu_time_coverage_counts_every_expected_device_without_charging_idle(consumer):
    state = consumer
    for seq, seconds in enumerate((10, 25), 1):
        value = resource_batch(seq=seq, observed_at=state.base + timedelta(seconds=seconds))
        inventory = value["resources"]["gpu"]["gpus"]
        inventory.append({**inventory[0], "gpu_uuid": "GPU-b", "index": 1})
        value["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert put(state.client, value).status_code == 200
    proofs = TraceAttestationService(state.sessions, state.service, clock=state.now)
    result = state.service.consume(CONTEXT, request(state, through_seq=2))
    proof = attest(proofs, state.service, state.service.exact(result.id))
    config = ProcessingCoverageConfiguration(
        trace=state.config,
        gpu_uuids=("GPU-b", "GPU-a"),
        recording=UsagePostingConfiguration(
            source_ref="two-gpu-window",
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
    window = UsageWindowQuery(
        node_id="node-a",
        start_at=state.base + timedelta(seconds=10),
        end_at=state.base + timedelta(seconds=25),
    )
    manifest = service.record(
        CONTEXT, capture_request(service, proof, window.start_at, window.end_at)
    ).manifest
    combined = combine(service, (manifest,), window)
    assert combined.total_usage_microseconds == 0 and combined.quota_usage_available
    assert combined.covered_gpu_microseconds == 30_000_000
    assert len(combined.cells) == 2
