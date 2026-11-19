from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event

from aris.core.contracts import DomainError
from aris.modules.lyra.partition_recording_schemas import PartitionRecordingRequest
from aris.modules.lyra.partition_recording_service import PartitionRecordingService
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_evidence_service import ResidencyRunEvidenceService
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionMethod,
    ResidencyPartitionReference,
)
from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
from aris.modules.lyra.residency_segment_service import ResidencySegmentService
from aris.modules.lyra.residency_segments import ResidencySegmentQuery
from aris.modules.lyra.run_recording_schemas import ClosedRunRecordingRequest
from aris.modules.lyra.run_recording_service import ClosedRunRecordingService
from aris.modules.lyra.segment_store_schemas import RecordResidencySegments
from aris.modules.lyra.segment_store_service import ResidencySegmentStore
from aris.modules.lyra.trace_attestation_schemas import TraceAttestationRequest
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
    UsageTraceConsumeRequest,
)
from aris.modules.lyra.trace_consumer_service import UsageTraceConsumerService
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from tests.test_administration import NOW, admin_state
from tests.test_closed_run_recording import counts
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_mapping_history import assign, history_state
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger"]
METHOD = ResidencyPartitionMethod(max_intervals=4)


@pytest.fixture
def partition_recording(ledger):
    source, original, history = ledger[2]
    sessions = history[1]
    clock = history[5]
    assert beat(ledger[4], heartbeat(seq=8)).status_code == 200
    for index in range(3, 10):
        payload = ownership(
            resource_batch(seq=3 + 2 * index, observed_at=NOW + timedelta(seconds=10 + 15 * index)),
            [owner(username="linux-user")],
        )
        assert put(ledger[4], payload).status_code == 200
        assert beat(ledger[4], heartbeat(seq=4 + 2 * index)).status_code == 200
    idle = resource_batch(seq=23, observed_at=NOW + timedelta(seconds=160))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(ledger[4], idle).status_code == beat(ledger[4], heartbeat(seq=24)).status_code == 200
    clock["now"] = max(datetime.now(UTC), clock["now"]) + timedelta(seconds=1)

    def now():
        return clock["now"]

    archive = VegaUsageArchive(sessions, clock=now)
    config = UsageTraceConfiguration(
        consumer_id="partition-recording-fixture",
        source_ref="explicit-partition-fixture",
        agent_id="agent-a",
        node_id="node-a",
        initial_after_seq=2,
        continuity=original.continuity,
        formation=original.formation,
    )
    replay = UsageTraceConsumerService(
        sessions, archive, source.observations, configuration=config, clock=now
    )
    attestations = TraceAttestationService(sessions, replay, clock=now)
    segments = ResidencySegmentStore(
        sessions,
        ResidencySegmentService(sessions, attestations, archive, source.observations, clock=now),
        clock=now,
    )
    while replay.position().after_seq < 24:
        receipt = replay.consume(
            UsageTraceConsumeContext(request_id="partition-fixture-trace"),
            UsageTraceConsumeRequest(
                id=uuid4(),
                expected_configuration_fingerprint=config.fingerprint,
                expected_revision=replay.position().revision,
                through_seq=24,
                limit=8,
            ),
        )
        record = replay.exact(receipt.id)
        proof = attestations.record(
            UsageTraceConsumeContext(request_id="partition-fixture-source"),
            TraceAttestationRequest(
                id=uuid4(),
                record_id=record.request.id,
                expected_record_fingerprint=record.fingerprint,
                source_cutoff=now(),
            ),
        ).attestation
        segments.record(
            POST,
            RecordResidencySegments(
                id=uuid4(),
                source=ResidencySegmentQuery(
                    attestation_id=proof.request.id,
                    expected_attestation_fingerprint=proof.fingerprint,
                ),
            ),
        )
    parent = ResidencyRunReference(
        record_id=record.request.id,
        expected_record_fingerprint=record.fingerprint,
        run_id=record.closed_runs[0].run.id,
        source_cutoff=now(),
    )
    evidence = ResidencyPartitionService(
        sessions, attestations, replay, segments, archive, method=METHOD, clock=now
    )
    reference = ResidencyPartitionReference(
        reference=parent,
        closing_attestation_id=proof.request.id,
        expected_closing_attestation_fingerprint=proof.fingerprint,
        expected_trace_configuration_fingerprint=config.fingerprint,
        expected_method_fingerprint=METHOD.fingerprint,
        partition_index=0,
    )
    request = PartitionRecordingRequest(
        id=uuid4(),
        partition=reference,
        expected_recording_configuration_fingerprint=ledger[3].fingerprint,
        identity_revision=original.identity_revision,
    )
    writer = PartitionRecordingService(sessions, evidence, history[6], source, ledger[0], clock=now)
    whole = ClosedRunRecordingService(
        sessions,
        ResidencyRunEvidenceService(sessions, archive, clock=now),
        archive,
        source,
        ledger[0],
        clock=now,
    )
    return writer, request, ledger, whole, clock


def part(request, index):
    return request.model_copy(
        update={
            "id": uuid4(),
            "partition": request.partition.model_copy(update={"partition_index": index}),
        }
    )


def postgres(state):
    if state[0].sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL atomic partition recording")


def test_all_balanced_parts_use_existing_ledger_and_exactly_retain_the_parent_window(
    partition_recording,
):
    writer, request, ledger, _, _ = partition_recording
    before = counts(partition_recording)
    receipts = [writer.record(POST, part(request, index)) for index in range(3)]
    entries = [ledger[0].exact(value.ledger.entries[0]) for value in receipts]
    assert all(
        value.ledger.effect == "recorded" and not value.quota_usage_available for value in receipts
    )
    assert [value.partition_index for value in receipts] == [0, 1, 2]
    assert sum(entry.details.measurement.observed_microseconds for entry in entries) == 135000000
    assert all(
        a.details.measurement.end_at == b.details.measurement.start_at
        for a, b in zip(entries, entries[1:], strict=False)
    )
    assert entries[0].details.measurement.start_at == NOW + timedelta(seconds=10)
    assert entries[-1].details.measurement.end_at == NOW + timedelta(seconds=145)
    assert counts(partition_recording)[:6] == tuple(
        a + b for a, b in zip(before[:6], (3, 3, 3, 3, 3, 1), strict=True)
    )


def test_same_key_paused_replay_new_key_dedup_and_kind_collision_are_explicit(partition_recording):
    writer, request, _, whole, _ = partition_recording
    first = writer.record(POST, request)
    before = counts(partition_recording)
    writer.ledger.configuration = None
    replay = writer.record(POST, request)
    assert replay.duplicate and replay.model_copy(update={"duplicate": False}) == first
    assert counts(partition_recording) == before
    writer.ledger.configuration = partition_recording[2][3]
    duplicate = writer.record(POST, request.model_copy(update={"id": uuid4()}))
    assert (
        duplicate.ledger.effect == "already_recorded"
        and duplicate.ledger.entries == first.ledger.entries
    )
    assert counts(partition_recording)[2] == 1
    wrong = ClosedRunRecordingRequest(
        id=request.id,
        reference=request.partition.reference,
        expected_trace_configuration_fingerprint=request.partition.expected_trace_configuration_fingerprint,
        expected_recording_configuration_fingerprint=request.expected_recording_configuration_fingerprint,
        identity_revision=request.identity_revision,
    )
    with pytest.raises(DomainError) as error:
        whole.record(POST, wrong)
    assert error.value.code == "CONFLICT"
    with pytest.raises(DomainError) as error:
        writer.record(
            POST,
            request.model_copy(
                update={"partition": request.partition.model_copy(update={"partition_index": 1})}
            ),
        )
    assert error.value.code == "CONFLICT"


def test_whole_window_and_parts_cannot_overlap_or_double_count(partition_recording):
    writer, request, _, whole, _ = partition_recording
    entire = ClosedRunRecordingRequest(
        id=uuid4(),
        reference=request.partition.reference,
        expected_trace_configuration_fingerprint=request.partition.expected_trace_configuration_fingerprint,
        expected_recording_configuration_fingerprint=request.expected_recording_configuration_fingerprint,
        identity_revision=request.identity_revision,
    )
    whole.record(POST, entire)
    before = counts(partition_recording)
    with pytest.raises(DomainError) as error:
        writer.record(POST, request)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(partition_recording) == before


def test_partitions_cannot_automatically_rebill_a_reversed_differently_sized_window(
    partition_recording,
):
    from tests.test_usage_reversals import adjustments, confirm, pending

    writer, request, ledger, _, _ = partition_recording
    state = adjustments.__wrapped__(ledger)
    confirm(state, pending(state))
    # Reversed [10,40) is smaller than partition zero [10,55).
    before = counts(partition_recording)
    with pytest.raises(DomainError) as error:
        writer.record(POST, request)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(partition_recording) == before
    assert writer.record(POST, part(request, 1)).ledger.effect == "recorded"


def test_late_part_cannot_bypass_the_entire_parent_recording_start(partition_recording):
    writer, request, ledger, _, _ = partition_recording
    config = ledger[3].model_copy(update={"recording_start_at": NOW + timedelta(seconds=60)})
    writer.ledger.configuration = config
    selected = part(request, 2).model_copy(
        update={"expected_recording_configuration_fingerprint": config.fingerprint}
    )
    before = counts(partition_recording)
    with pytest.raises(DomainError) as error:
        writer.record(POST, selected)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(partition_recording) == before


def test_parent_mapping_change_blocks_even_a_locally_resolvable_late_partition(partition_recording):
    writer, request, ledger, _, clock = partition_recording
    history = ledger[2][2]
    present = clock["now"]
    changed = assign(history, 80, 1, expected=history[4][0])
    clock["now"] = present
    selected = part(request, 2).model_copy(update={"identity_revision": changed.revision})
    before = counts(partition_recording)
    with pytest.raises(DomainError) as error:
        writer.record(POST, selected)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(partition_recording) == before


@pytest.mark.parametrize("failure", ["source", "identity", "configuration"])
def test_final_parent_and_source_verification_failure_rolls_back_every_partition_effect(
    partition_recording, monkeypatch, failure
):
    writer, request, _, _, _ = partition_recording
    before = counts(partition_recording)
    if failure == "source":
        original = writer._proof
        calls = []

        def changed(*args):
            calls.append(True)
            if len(calls) == 2:
                raise DomainError("CONFLICT", "fixture final partition source changed", 409)
            return original(*args)

        monkeypatch.setattr(writer, "_proof", changed)
    elif failure == "identity":
        original = writer._identity
        calls = []

        def changed(*args):
            calls.append(True)
            if len(calls) == 2:
                raise DomainError("CONFLICT", "fixture final parent identity changed", 409)
            return original(*args)

        monkeypatch.setattr(writer, "_identity", changed)
    else:
        original = writer._emit

        def changed(*args):
            original(*args)
            writer.ledger.configuration = None

        monkeypatch.setattr(writer, "_emit", changed)
    with pytest.raises(DomainError):
        writer.record(POST, request)
    assert counts(partition_recording) == before


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_partition_commit_recovers_original_binding_and_single_measurement(
    partition_recording, monkeypatch, when
):
    import aris.modules.lyra.partition_recording_service as module

    writer, request, _, _, _ = partition_recording
    before = counts(partition_recording)[:6]
    original = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            original(session)
        raise RuntimeError("fixture partition commit outcome unknown")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", interrupted)
        with pytest.raises(RuntimeError):
            writer.record(POST, request)
    assert writer.record(POST, request).duplicate == (when == "after")
    assert counts(partition_recording)[:6] == tuple(
        a + b for a, b in zip(before, (1, 1, 1, 1, 1, 1), strict=True)
    )


def test_shared_binding_insert_failure_cannot_leave_a_measurement_without_its_partition(
    partition_recording,
):
    writer, request, _, _, _ = partition_recording
    before = counts(partition_recording)
    engine = writer.sessions.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO usage_run_recordings"):
            raise RuntimeError("fixture binding interrupted")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            writer.record(POST, request)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(partition_recording) == before
    with writer.sessions() as session:
        writer.record_in_transaction(session, POST, request)
        session.rollback()
    assert counts(partition_recording) == before


def test_existing_fixed_queue_dispatches_partitions_only_with_an_explicit_handler(
    partition_recording,
):
    writer, request, _, whole, clock = partition_recording
    limits = RecordingJobLimits(
        transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
    )
    queue = RecordingJobService(writer.sessions, whole, limits=limits, clock=lambda: clock["now"])
    assert queue.enqueue(POST, request).job.state == "queued"
    assert queue.claim(POST) is None and queue.exact(request.id).attempts == 0
    full = ClosedRunRecordingRequest(
        id=uuid4(),
        reference=request.partition.reference,
        expected_trace_configuration_fingerprint=request.partition.expected_trace_configuration_fingerprint,
        expected_recording_configuration_fingerprint=request.expected_recording_configuration_fingerprint,
        identity_revision=request.identity_revision,
    )
    queue.enqueue(POST, full)
    assert queue.claim(POST).id == full.id
    queue.partition_recorder = writer
    result = queue.execute(POST, queue.claim(POST))
    assert result.state == "completed" and result.recording_id == request.id
    queue.partition_recorder = None
    assert queue.exact(request.id).state == "completed" and queue.enqueue(POST, request).duplicate


def test_partition_inputs_never_supply_owner_amount_or_derived_session_index(partition_recording):
    _, request, _, _, _ = partition_recording
    for changes in (
        {"owner_id": str(uuid4())},
        {"observed_microseconds": 1},
        {"session_index": 0},
        {"actor": "system:lyra"},
    ):
        with pytest.raises(ValidationError):
            PartitionRecordingRequest.model_validate({**request.model_dump(), **changes})


def test_postgresql_parallel_different_parts_commit_adjacent_measured_claims(partition_recording):
    writer, request, ledger, _, _ = partition_recording
    postgres(partition_recording)
    gate = Barrier(2)

    def run(index):
        gate.wait(timeout=5)
        return writer.record(POST, part(request, index))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert all(result.ledger.effect == "recorded" for result in results)
    assert counts(partition_recording)[2:4] == (2, 2)
    assert (
        sum(
            ledger[0].exact(result.ledger.entries[0]).details.measurement.observed_microseconds
            for result in results
        )
        == 90000000
    )


def test_postgresql_same_partition_key_is_exactly_replayed_under_concurrency(partition_recording):
    writer, request, _, _, _ = partition_recording
    postgres(partition_recording)
    gate = Barrier(2)

    def run(_):
        gate.wait(timeout=5)
        return writer.record(POST, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(result.duplicate for result in results) == [False, True]
    assert counts(partition_recording)[0] == counts(partition_recording)[2] == 1
