"""Classify sampled edges against committed closed recordings and identity evidence."""

from datetime import datetime
from uuid import UUID

from aris.modules.lyra.coverage_ledger import (
    CoverageLedger,
    CoverageLedgerEntry,
    coverage_entry,
    original_coverage_ledger,
)
from aris.modules.lyra.coverage_replay import CoverageEdge, CoverageReplay
from aris.modules.lyra.coverage_schemas import (
    CoverageIdentity,
    CoverageRecordingReference,
    ProcessingCoverageAnalysis,
    ProcessingCoverageCell,
    ProcessingCoverageConfiguration,
    ProcessingCoverageRequest,
    coverage_identity_key,
    processing_coverage_fingerprint,
)
from aris.modules.lyra.ledger_schemas import UsageLedgerEntry
from aris.modules.lyra.partition_recording_schemas import StoredPartitionRecording
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.run_recording_schemas import StoredClosedRunRecording
from aris.modules.lyra.stable_window_recording_schemas import StableWindowRecordingEvidence
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation

type CoverageRecording = (
    StoredClosedRunRecording | StoredPartitionRecording | StableWindowRecordingEvidence
)


def recording_parent(value: CoverageRecording) -> ResidencyRun:
    if isinstance(value, StableWindowRecordingEvidence):
        return value.acceptance.proof.original.parent_run
    return (
        value.proof.run if isinstance(value, StoredClosedRunRecording) else value.proof.parent_run
    )


def recording_id(value: CoverageRecording) -> UUID:
    return (
        value.recording.id if isinstance(value, StableWindowRecordingEvidence) else value.request.id
    )


def recording_reference(
    value: CoverageRecording,
    entry: UsageLedgerEntry | CoverageLedgerEntry,
    config: ProcessingCoverageConfiguration,
    head_entries: tuple[UsageLedgerEntry, ...] | CoverageLedger,
) -> CoverageRecordingReference | None:
    if isinstance(entry, UsageLedgerEntry):
        entry = coverage_entry(entry)
    parent = recording_parent(value)
    if isinstance(value, StableWindowRecordingEvidence):
        trace_fp = value.acceptance.request.window.expected_trace_configuration_fingerprint
        recording_fp = value.acceptance.configuration.fingerprint
        candidate = value.acceptance.proof.original.candidate
        first, last = candidate.evidence[0].previous, candidate.evidence[-1].current
        proof_fp = value.acceptance.proof.fingerprint
    else:
        recording_fp = value.request.expected_recording_configuration_fingerprint
        proof_fp = value.proof.fingerprint
        first, last = (
            (parent.first, parent.last)
            if isinstance(value, StoredClosedRunRecording)
            else (
                value.proof.candidate.evidence[0].previous,
                value.proof.candidate.evidence[-1].current,
            )
        )
        trace_fp = (
            value.proof.configuration_fingerprint
            if isinstance(value, StoredClosedRunRecording)
            else value.request.partition.expected_trace_configuration_fingerprint
        )
    receipt = value.ledger
    committed_head = (
        head_entries.contains(receipt.head)
        if isinstance(head_entries, CoverageLedger)
        else receipt.head.revision > 0
        and receipt.head.revision <= len(head_entries)
        and head_entries[receipt.head.revision - 1].fingerprint == receipt.head.fingerprint
    )
    if (
        trace_fp != config.trace.fingerprint
        or recording_fp != config.recording.fingerprint
        or (parent.first.agent_id, parent.first.node_id)
        != (config.trace.agent_id, config.trace.node_id)
        or parent.rule_fingerprint != config.recording.timing_fingerprint
        or entry.id not in receipt.entries
        or receipt.head.revision == 0
        or not committed_head
        or receipt.entry_fingerprints[receipt.entries.index(entry.id)] != entry.fingerprint
    ):
        return None
    measured = entry.measurement
    if (
        measured.gpu_uuid,
        measured.start_at,
        measured.end_at,
        measured.model,
        measured.timing_fingerprint,
    ) != (
        parent.gpu_uuid,
        first.compute_observed_at,
        last.compute_observed_at,
        parent.model,
        parent.rule_fingerprint,
    ):
        return None
    return CoverageRecordingReference(
        id=recording_id(value),
        fingerprint=value.fingerprint,
        run_id=parent.id,
        proof_fingerprint=proof_fp,
        entry_id=entry.id,
        entry_fingerprint=entry.fingerprint,
    )


def processing_ledger_range(
    replay: CoverageReplay, start_at: datetime, end_at: datetime, recording_start_at: datetime
) -> tuple[datetime, datetime] | None:
    ranges = [
        (max(start_at, edge.start_at, recording_start_at), min(end_at, edge.end_at))
        for edge in replay.edges
        if edge.gpu.state in {"stable_owner", "observed_idle"}
        and max(start_at, edge.start_at, recording_start_at) < min(end_at, edge.end_at)
        and not any(edge.start_at <= value <= edge.end_at for value in replay.invalid_times)
    ]
    return (
        (min(value[0] for value in ranges), max(value[1] for value in ranges)) if ranges else None
    )


def processing_coverage(
    request: ProcessingCoverageRequest,
    configuration: ProcessingCoverageConfiguration,
    attestation: StoredTraceAttestation,
    replay: CoverageReplay,
    entries: tuple[UsageLedgerEntry, ...],
    recordings: tuple[CoverageRecording, ...],
    identities: tuple[CoverageIdentity, ...],
    *,
    read_at: datetime,
    ledger_context: CoverageLedger | None = None,
) -> ProcessingCoverageAnalysis:
    indexed = configuration.method == "indexed_sampled_residency_edges"
    if indexed != (ledger_context is not None):
        raise ValueError("processing coverage requires its configured ledger evidence method")
    ledger = ledger_context or original_coverage_ledger(request.ledger, entries, read_at)
    if ledger.head != request.ledger or ledger.read_at != read_at or ledger.indexed != indexed:
        raise ValueError("processing coverage ledger boundary differs")
    if indexed:
        required = processing_ledger_range(
            replay, request.start_at, request.end_at, configuration.recording.recording_start_at
        )
        selection = ledger.selection
        if (required is None and (selection is not None or ledger.active or ledger.reversals)) or (
            required is not None
            and (
                selection is None
                or (selection.start_at, selection.end_at) != required
                or selection.snapshot != request.ledger
                or selection.owner_id is not None
            )
        ):
            raise ValueError("processing ledger selection does not cover its original edges")
    active = ledger.active
    by_identity = {
        coverage_identity_key(value.run): index for index, value in enumerate(identities)
    }
    latest_identity: dict[str, int] = {}
    for index, value in enumerate(identities):
        previous = latest_identity.get(value.run.id)
        if previous is None or identities[previous].run.intervals < value.run.intervals:
            latest_identity[value.run.id] = index
    refs: list[CoverageRecordingReference] = []
    by_recording: dict[tuple[str, UUID], int] = {}
    recording_identities: dict[int, int] = {}
    stable_references: set[int] = set()
    for recording in recordings:
        for entry in active:
            if entry.id not in recording.ledger.entries:
                continue
            ref = recording_reference(recording, entry, configuration, ledger)
            if ref is not None and (ref.run_id, entry.id) not in by_recording:
                by_recording[(ref.run_id, entry.id)] = len(refs)
                parent_index = by_identity.get(coverage_identity_key(recording_parent(recording)))
                if parent_index is not None:
                    recording_identities[len(refs)] = parent_index
                if isinstance(recording, StableWindowRecordingEvidence):
                    stable_references.add(len(refs))
                refs.append(ref)
    cells = []
    for gpu_uuid in configuration.gpu_uuids:
        edges = [
            edge
            for edge in replay.edges
            if edge.gpu.gpu_uuid == gpu_uuid
            and edge.start_at < request.end_at
            and edge.end_at > request.start_at
        ]
        current = [
            entry
            for entry in active
            if entry.measurement.gpu_uuid == gpu_uuid
            and entry.measurement.start_at < request.end_at
            and entry.measurement.end_at > request.start_at
            and (
                not indexed
                or any(
                    edge.start_at < entry.measurement.end_at
                    and edge.end_at > entry.measurement.start_at
                    for edge in edges
                )
            )
        ]
        reversals = [
            entry.measurement
            for entry in ledger.reversals
            if entry.measurement.gpu_uuid == gpu_uuid
        ]
        boundaries = {request.start_at, request.end_at}
        for window_edge in edges:
            boundaries.update(
                (
                    max(request.start_at, window_edge.start_at),
                    min(request.end_at, window_edge.end_at),
                )
            )
        for entry in current:
            boundaries.update(
                (
                    max(request.start_at, entry.measurement.start_at),
                    min(request.end_at, entry.measurement.end_at),
                )
            )
        windows = [(value.start_at, value.end_at) for value in replay.hazards]
        windows.extend((value.start_at, value.end_at) for value in reversals)
        for window_start, window_end in windows:
            if window_start < request.end_at and window_end > request.start_at:
                boundaries.update(
                    (max(request.start_at, window_start), min(request.end_at, window_end))
                )
        if request.start_at < configuration.recording.recording_start_at < request.end_at:
            boundaries.add(configuration.recording.recording_start_at)
        ordered = sorted(boundaries)
        for start, end in zip(ordered, ordered[1:], strict=False):
            candidates = [edge for edge in edges if edge.start_at <= start and edge.end_at >= end]
            overlaps = [
                entry
                for entry in current
                if entry.measurement.start_at < end and entry.measurement.end_at > start
            ]
            state, reason = "uncovered", "SOURCE_BOUNDARY_NOT_OBSERVED"
            edge: CoverageEdge | None = candidates[0] if len(candidates) == 1 else None
            owner = None
            recording_index = None
            identity_index = latest_identity.get(edge.run.id) if edge and edge.run else None
            if start < configuration.recording.recording_start_at:
                reason = "BEFORE_RECORDING_START"
            elif any(value.start_at < end and value.end_at > start for value in replay.hazards):
                state, reason = "unknown", "SOURCE_TIME_OR_QUALITY_UNCERTAIN"
            elif len(candidates) > 1:
                state, reason = "unknown", "SOURCE_TIME_OVERLAP"
            elif edge is not None:
                state, reason = "unknown", edge.gpu.reason
                if any(edge.start_at <= value <= edge.end_at for value in replay.invalid_times):
                    reason = "INVALID_SOURCE_OBSERVATION"
                elif edge.gpu.state == "observed_idle":
                    if overlaps or any(
                        value.start_at < end and value.end_at > start for value in reversals
                    ):
                        reason = "LEDGER_SOURCE_CONFLICT"
                    else:
                        state, reason = "observed_idle", "NO_COMPUTE_APPS_AT_ENDPOINTS"
                elif edge.gpu.state == "stable_owner":
                    reference_index = (
                        by_recording.get((edge.run.id, overlaps[0].id))
                        if edge.run and len(overlaps) == 1
                        else None
                    )
                    if reference_index is not None:
                        identity_index = recording_identities.get(reference_index)
                    identity = (
                        identities[identity_index].identity if identity_index is not None else None
                    )
                    if identity is None or identity.status != "resolved":
                        reason = "IDENTITY_UNRESOLVED"
                    elif not overlaps:
                        reason = (
                            "REVERSED_AWAITING_REVIEW"
                            if any(
                                value.start_at < end and value.end_at > start for value in reversals
                            )
                            else "RESIDENCY_NOT_RECORDED"
                        )
                    else:
                        entry = overlaps[0]
                        measurement = entry.measurement
                        assert entry.kind == "measurement"
                        if (
                            measurement.owner_id != identity.user_id
                            or measurement.model != edge.gpu.model
                            or measurement.timing_fingerprint
                            != configuration.recording.timing_fingerprint
                        ):
                            reason = "LEDGER_IDENTITY_OR_METHOD_CONFLICT"
                        elif reference_index is None:
                            reason = "CLOSED_RECORDING_NOT_VERIFIED"
                        else:
                            state, reason = (
                                "recorded",
                                (
                                    "COMMITTED_STABLE_RECORDING"
                                    if reference_index in stable_references
                                    else "COMMITTED_CLOSED_RECORDING"
                                ),
                            )
                            owner, recording_index = identity.user_id, reference_index
            cells.append(
                ProcessingCoverageCell(
                    gpu_uuid=gpu_uuid,
                    start_at=start,
                    end_at=end,
                    state=state,
                    reason=reason,
                    previous=edge.previous if edge else None,
                    current=edge.current if edge else None,
                    run_id=edge.run.id if edge and edge.run else None,
                    identity_index=identity_index,
                    recording_index=recording_index,
                    owner_id=owner,
                )
            )
    known = sum(value.microseconds for value in cells if value.state == "recorded")
    unknown = sum(value.microseconds for value in cells if value.state == "unknown")
    uncovered = sum(value.microseconds for value in cells if value.state == "uncovered")
    result = ProcessingCoverageAnalysis.model_construct(
        request=request,
        configuration=configuration,
        attestation=attestation,
        observations=replay.observations,
        hazards=replay.hazards,
        invalid_observation_times=replay.invalid_times,
        identities=identities,
        recordings=tuple(refs),
        cells=tuple(cells),
        identity_revision=identities[0].identity.directory_revision if identities else None,
        known_recorded_microseconds=known,
        unknown_gpu_microseconds=unknown,
        uncovered_gpu_microseconds=uncovered,
        complete_within_source_window=unknown == uncovered == 0,
        read_at=read_at,
        fingerprint="0" * 64,
    )
    return ProcessingCoverageAnalysis.model_validate(
        {**result.model_dump(mode="json"), "fingerprint": processing_coverage_fingerprint(result)}
    )
