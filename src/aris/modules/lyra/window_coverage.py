"""Compose source diagnostics and current accounting without filling unknown time with zero."""

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_schemas import (
    CoverageIdentity,
    ProcessingCoverageConfiguration,
    StoredProcessingCoverage,
)
from aris.modules.lyra.ledger_schemas import ReversedMeasurement, UsageLedgerEntry, UsageLedgerHead
from aris.modules.lyra.recorded_usage import _active_prefix
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_schemas import canonical_trace
from aris.modules.lyra.window_schemas import (
    MAX_WINDOW_CELLS,
    MAX_WINDOW_MANIFESTS,
    MAX_WINDOW_SOURCE_BYTES,
    MAX_WINDOW_SOURCE_CELLS,
    MICROSECOND,
    CoverageManifestReference,
    SampledUsageWindow,
    UsageWindowQuery,
    WindowCoverageCell,
    usage_window_fingerprint,
)
from aris.modules.vega.usage_archive_schemas import UsageArchiveHead


@dataclass(frozen=True)
class _Candidate:
    gpu_uuid: str
    start_at: datetime
    end_at: datetime
    state: str
    reason: str
    owner_id: UUID | None
    entry_id: UUID | None


def _prefix(
    manifests: tuple[StoredProcessingCoverage, ...],
    config: ProcessingCoverageConfiguration,
    source: UsageArchiveHead,
    ledger: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    read_at: datetime,
) -> bool:
    attestations: dict[UUID, StoredTraceAttestation] = {}
    windows: dict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)
    seen = set()
    source_bytes = 0
    for manifest in manifests:
        raw = canonical_trace(manifest)
        source_bytes += len(raw.encode())
        if source_bytes > MAX_WINDOW_SOURCE_BYTES:
            raise DomainError(
                "USAGE_REPORT_TOO_LARGE", "Window source manifests exceed the byte budget", 503
            )
        StoredProcessingCoverage.model_validate_json(raw)
        analysis, request = manifest.analysis, manifest.analysis.request
        proof = analysis.attestation
        if (
            request.id in seen
            or analysis.configuration != config
            or manifest.created_at > read_at
            or request.ledger.revision > ledger.revision
            or (
                request.ledger.revision > 0
                and entries[request.ledger.revision - 1].fingerprint != request.ledger.fingerprint
            )
            or proof.last_seq > source.highest_committed_seq
        ):
            raise DomainError("CONFLICT", "Window manifest scope or committed prefix differs", 409)
        seen.add(request.id)
        if proof.request.id in attestations and attestations[proof.request.id] != proof:
            raise DomainError("CONFLICT", "Window source attestations disagree", 409)
        attestations[proof.request.id] = proof
        windows[proof.request.id].append((request.start_at, request.end_at))
    for values in windows.values():
        ordered = sorted(values)
        if any(
            previous[1] > following[0]
            for previous, following in zip(ordered, ordered[1:], strict=False)
        ):
            raise DomainError(
                "CONFLICT", "Select nonoverlapping versions for each source segment", 409
            )
    expected, revision = config.trace.initial_after_seq + 1, 1
    previous_id = previous_fingerprint = None
    complete = True
    for proof in sorted(attestations.values(), key=lambda value: value.first_seq):
        if (
            proof.first_seq != expected
            or proof.revision != revision
            or proof.previous_id != previous_id
            or proof.previous_fingerprint != previous_fingerprint
        ):
            complete = False
        expected, revision = proof.last_seq + 1, proof.revision + 1
        previous_id, previous_fingerprint = proof.request.id, proof.fingerprint
    return complete and expected == source.highest_committed_seq + 1


def _candidates(
    manifests: tuple[StoredProcessingCoverage, ...],
    active: tuple[UsageLedgerEntry, ...],
    identities: tuple[CoverageIdentity, ...],
    identity_revision: int | None,
    read_at: datetime,
    config: ProcessingCoverageConfiguration,
) -> tuple[tuple[_Candidate, ...], tuple[tuple[datetime, datetime], ...]]:
    invalid = sorted(
        {time for value in manifests for time in value.analysis.invalid_observation_times}
    )
    hazards = tuple(
        sorted(
            {
                (hazard.start_at, hazard.end_at)
                for value in manifests
                for hazard in value.analysis.hazards
            }
        )
    )
    by_identity = {value.run.id: value for value in identities}
    for value in identities:
        CoverageIdentity.model_validate(value.model_dump(mode="json"))
    if len(by_identity) != len(identities) or any(
        value.identity.directory_revision != identity_revision
        or value.identity.query.as_of != read_at
        for value in identities
    ):
        raise DomainError("CONFLICT", "Window identity snapshot is inconsistent", 409)
    by_entry = {entry.id: entry for entry in active}
    result = []
    for manifest in manifests:
        analysis = manifest.analysis
        for cell in analysis.cells:
            if cell.state == "uncovered":
                continue  # A local source boundary may be filled by a neighbouring segment.
            state, reason = cell.state, cell.reason
            owner, entry_id = cell.owner_id, None
            if cell.previous is not None and cell.current is not None:
                first = analysis.observations[cell.previous].compute_observed_at
                last = analysis.observations[cell.current].compute_observed_at
                index = bisect_left(invalid, first)
                if index < len(invalid) and invalid[index] <= last:
                    state, reason = "unknown", "INVALID_SOURCE_OBSERVATION"
            if state == "recorded":
                assert cell.recording_index is not None and cell.identity_index is not None
                reference = analysis.recordings[cell.recording_index]
                entry_id = reference.entry_id
                entry = by_entry.get(entry_id)
                parent = analysis.identities[cell.identity_index].run
                identity = by_identity.get(parent.id)
                if entry is None:
                    state, reason = "unknown", "REVERSED_AWAITING_REVIEW"
                elif (
                    entry.fingerprint != reference.entry_fingerprint
                    or entry.details.measurement.owner_id != owner
                    or entry.details.measurement.gpu_uuid != cell.gpu_uuid
                    or entry.details.measurement.model != parent.model
                    or entry.details.measurement.timing_fingerprint
                    != config.recording.timing_fingerprint
                ):
                    raise DomainError(
                        "CONFLICT", "Window recorded entry differs from its receipt", 409
                    )
                elif identity is not None and identity.run != parent:
                    raise DomainError(
                        "CONFLICT", "Window identity does not assess the whole closed parent", 409
                    )
                elif identity is None or identity.identity.status != "resolved":
                    state, reason = "unknown", "IDENTITY_UNRESOLVED"
                elif identity.identity.user_id != owner:
                    state, reason = "unknown", "LEDGER_IDENTITY_OR_METHOD_CONFLICT"
            result.append(
                _Candidate(
                    cell.gpu_uuid,
                    cell.start_at,
                    cell.end_at,
                    state,
                    reason,
                    owner if state == "recorded" else None,
                    entry_id,
                )
            )
    return tuple(result), hazards


def _gpu_cells(
    gpu: str,
    query: UsageWindowQuery,
    config: ProcessingCoverageConfiguration,
    sources: tuple[_Candidate, ...],
    hazards: tuple[tuple[datetime, datetime], ...],
    active: tuple[UsageLedgerEntry, ...],
    entries: tuple[UsageLedgerEntry, ...],
    global_reason: str | None,
) -> list[WindowCoverageCell]:
    candidates = [value for value in sources if value.gpu_uuid == gpu]
    measurements = [entry for entry in active if entry.details.measurement.gpu_uuid == gpu]
    reversals = [
        entry.details.measurement
        for entry in entries
        if isinstance(entry.details, ReversedMeasurement)
        and entry.details.measurement.gpu_uuid == gpu
    ]
    events: dict[datetime, list[tuple[int, int, bool]]] = defaultdict(list)
    events[query.start_at], events[query.end_at] = [], []

    def add(group: int, index: int, start: datetime, end: datetime) -> None:
        first, last = max(start, query.start_at), min(end, query.end_at)
        if first < last:
            events[first].append((group, index, True))
            events[last].append((group, index, False))

    for index, cell in enumerate(candidates):
        add(0, index, cell.start_at, cell.end_at)
    for index, measurement in enumerate(measurements):
        value = measurement.details.measurement
        add(1, index, value.start_at, value.end_at)
    for index, value in enumerate(reversals):
        add(2, index, value.start_at, value.end_at)
    for index, (start, end) in enumerate(hazards):
        add(3, index, start, end)
    if query.start_at < config.recording.recording_start_at < query.end_at:
        events[config.recording.recording_start_at]  # A boundary, without inventing a source event.
    positions = sorted(events)
    present: list[set[int]] = [set(), set(), set(), set()]
    result: list[WindowCoverageCell] = []
    for start, end in zip(positions, positions[1:], strict=False):
        for group, index, adding in events[start]:
            if adding:
                present[group].add(index)
            else:
                present[group].remove(index)
        state, reason, owner = "uncovered", "SOURCE_BOUNDARY_NOT_OBSERVED", None
        if start < config.recording.recording_start_at:
            reason = "BEFORE_RECORDING_START"
        elif global_reason is not None:
            state, reason = "unknown", global_reason
        elif present[3]:
            state, reason = "unknown", "SOURCE_TIME_OR_QUALITY_UNCERTAIN"
        elif len(present[0]) > 1:
            state, reason = "unknown", "SOURCE_TIME_OVERLAP"
        elif present[0]:
            cell = candidates[next(iter(present[0]))]
            state, reason, owner = cell.state, cell.reason, cell.owner_id
            if state == "observed_idle" and (present[1] or present[2]):
                state, reason = "unknown", "LEDGER_SOURCE_CONFLICT"
            elif state == "recorded" and {measurements[index].id for index in present[1]} != {
                cell.entry_id
            }:
                state, reason, owner = "unknown", "LEDGER_SOURCE_CONFLICT", None
        output = WindowCoverageCell(
            gpu_uuid=gpu, start_at=start, end_at=end, state=state, reason=reason, owner_id=owner
        )
        if result and (result[-1].state, result[-1].reason, result[-1].owner_id) == (
            state,
            reason,
            owner,
        ):
            result[-1] = result[-1].model_copy(update={"end_at": end})
        else:
            result.append(output)
            if len(result) > MAX_WINDOW_CELLS:
                raise DomainError(
                    "USAGE_REPORT_TOO_LARGE", "Window has too many coverage changes", 503
                )
    return result


def sampled_usage_window(
    query: UsageWindowQuery,
    configuration: ProcessingCoverageConfiguration,
    source: UsageArchiveHead,
    ledger: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    manifests: tuple[StoredProcessingCoverage, ...],
    identities: tuple[CoverageIdentity, ...],
    *,
    identity_revision: int | None,
    read_at: datetime,
) -> SampledUsageWindow:
    if read_at.tzinfo is None or read_at.utcoffset() is None:
        raise ValueError("usage window requires an aware read time")
    read_at = read_at.astimezone(UTC)
    query = UsageWindowQuery.model_validate(query.model_dump(mode="json"))
    configuration = ProcessingCoverageConfiguration.model_validate(
        configuration.model_dump(mode="json")
    )
    source = UsageArchiveHead.model_validate(source.model_dump(mode="json"))
    if (
        len(manifests) > MAX_WINDOW_MANIFESTS
        or sum(len(value.analysis.cells) for value in manifests) > MAX_WINDOW_SOURCE_CELLS
    ):
        raise DomainError(
            "USAGE_REPORT_TOO_LARGE", "Window processing exceeds its explicit budget", 503
        )
    if (
        query.node_id != ledger.node_id
        or query.node_id != configuration.trace.node_id
        or (source.query.agent_id, source.query.node_id)
        != (configuration.trace.agent_id, query.node_id)
        or source.highest_committed_seq < configuration.trace.initial_after_seq
        or read_at < max(query.end_at, source.read_at)
    ):
        raise DomainError("CONFLICT", "Window source, ledger or time binding differs", 409)
    active = _active_prefix(ledger, entries, read_at)
    prefix = _prefix(manifests, configuration, source, ledger, entries, read_at)
    candidates, hazards = _candidates(
        manifests, active, identities, identity_revision, read_at, configuration
    )
    global_reason = None if prefix else "SOURCE_PREFIX_INCOMPLETE"
    if any(
        entry.details.measurement.gpu_uuid not in configuration.gpu_uuids
        and entry.details.measurement.start_at < query.end_at
        and entry.details.measurement.end_at > query.start_at
        for entry in active
    ):
        global_reason = "LEDGER_GPU_INVENTORY_CONFLICT"
    cells = tuple(
        cell
        for gpu in configuration.gpu_uuids
        for cell in _gpu_cells(
            gpu, query, configuration, candidates, hazards, active, entries, global_reason
        )
    )
    if len(cells) > MAX_WINDOW_CELLS:
        raise DomainError("USAGE_REPORT_TOO_LARGE", "Window has too many coverage changes", 503)
    known = sum(cell.microseconds for cell in cells if cell.state in ("observed_idle", "recorded"))
    unknown = sum(cell.microseconds for cell in cells if cell.state == "unknown")
    uncovered = sum(cell.microseconds for cell in cells if cell.state == "uncovered")
    verified = sum(
        cell.microseconds
        for cell in cells
        if cell.state == "recorded" and (query.owner_id is None or query.owner_id == cell.owner_id)
    )
    recorded = sum(
        (min(query.end_at, value.end_at) - max(query.start_at, value.start_at)) // MICROSECOND
        for entry in active
        if (value := entry.details.measurement).start_at < query.end_at
        and value.end_at > query.start_at
        and (query.owner_id is None or value.owner_id == query.owner_id)
    )
    complete = prefix and unknown == uncovered == 0
    references = tuple(
        CoverageManifestReference(
            id=value.analysis.request.id,
            fingerprint=value.fingerprint,
            attestation_id=value.analysis.request.attestation_id,
            attestation_fingerprint=value.analysis.attestation.fingerprint,
            first_seq=value.analysis.attestation.first_seq,
            last_seq=value.analysis.attestation.last_seq,
        )
        for value in sorted(
            manifests,
            key=lambda value: (
                value.analysis.attestation.first_seq,
                value.analysis.request.start_at,
            ),
        )
    )
    result = SampledUsageWindow.model_construct(
        query=query,
        configuration=configuration,
        source=source,
        ledger=ledger,
        identity_revision=identity_revision,
        manifests=references,
        source_prefix_complete=prefix,
        coverage="complete" if complete else ("partial" if known else "unknown"),
        quota_usage_available=complete,
        recorded_microseconds=recorded,
        verified_usage_microseconds=verified,
        total_usage_microseconds=verified if complete else None,
        covered_gpu_microseconds=known,
        unknown_gpu_microseconds=unknown,
        uncovered_gpu_microseconds=uncovered,
        cells=cells,
        read_at=read_at,
        fingerprint="0" * 64,
    )
    return SampledUsageWindow.model_validate(
        {**result.model_dump(mode="json"), "fingerprint": usage_window_fingerprint(result)}
    )
