"""Resolve bounded closed-receipt and historical identity inputs for source coverage."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from aris.core.access.history_schemas import HistoricalNodeUserBatch, HistoricalNodeUserQuery
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_ledger import CoverageLedger, CoverageLedgerEntry, coverage_entry
from aris.modules.lyra.coverage_replay import CoverageReplay
from aris.modules.lyra.coverage_schemas import (
    CoverageIdentity,
    ProcessingCoverageConfiguration,
    coverage_identity_key,
)
from aris.modules.lyra.ledger_schemas import UsageLedgerEntry, UsageLedgerHead
from aris.modules.lyra.processing_coverage import (
    CoverageRecording,
    recording_id,
    recording_parent,
    recording_reference,
)
from aris.modules.lyra.recorded_usage import _active_prefix
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository
from aris.modules.lyra.stable_window_recording_repo import StableWindowRecordingRepository
from aris.modules.lyra.stable_window_recording_schemas import StableWindowRecordingEvidence


def get_coverage_recording(session: Session, id: UUID) -> CoverageRecording | None:
    stable = StableWindowRecordingRepository().evidence_for_operation(session, id)
    return stable if stable is not None else ClosedRunRecordingRepository(session).get_any(id)


def coverage_parents(
    replay: CoverageReplay,
    start_at: datetime,
    end_at: datetime,
    recordings: tuple[CoverageRecording, ...],
    entries: tuple[UsageLedgerEntry, ...],
    configuration: ProcessingCoverageConfiguration,
    *,
    ledger_context: CoverageLedger | None = None,
) -> tuple[ResidencyRun, ...]:
    runs: dict[str, ResidencyRun] = {}
    for edge in replay.edges:
        if edge.run is not None and edge.start_at < end_at and edge.end_at > start_at:
            previous = runs.get(edge.run.id)
            if previous is None or edge.run.intervals > previous.intervals:
                runs[edge.run.id] = edge.run
    stable_parents: dict[tuple[str, str, int, str], ResidencyRun] = {}
    for recording in recordings:
        parent = recording_parent(recording)
        local = runs.get(parent.id)
        if local is None:
            raise DomainError("CONFLICT", "Coverage receipt has no relevant source residency", 409)
        if not any(
            recording_reference(recording, entry, configuration, ledger_context or entries)
            for entry in (ledger_context.active if ledger_context is not None else entries)
            if entry.id in recording.ledger.entries
        ):
            raise DomainError("CONFLICT", "Coverage receipt does not bind the captured ledger", 409)
        if (
            parent.first != local.first
            or parent.rule_fingerprint != local.rule_fingerprint
            or (
                not isinstance(recording, StableWindowRecordingEvidence)
                and (parent.last.seq < local.last.seq or parent.intervals < local.intervals)
            )
            or (parent.intervals == local.intervals and parent != local)
        ):
            raise DomainError("CONFLICT", "Coverage closed parent differs from its source run", 409)
        if isinstance(recording, StableWindowRecordingEvidence):
            stable_parents[coverage_identity_key(parent)] = parent
        else:
            runs[parent.id] = parent
    parents = {coverage_identity_key(run): run for run in runs.values()}
    parents.update(stable_parents)
    if len(parents) > 128:
        raise DomainError("USAGE_REPORT_TOO_LARGE", "Split the coverage time window", 503)
    return tuple(parents[key] for key in sorted(parents))


def find_coverage_recordings(
    session: Session,
    replay: CoverageReplay,
    start_at: datetime,
    end_at: datetime,
    head: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    configuration: ProcessingCoverageConfiguration,
    read_at: datetime,
    *,
    through_recording_revision: int | None = None,
) -> tuple[CoverageRecording, ...]:
    active = _active_prefix(head, entries, read_at)
    result = coverage_recording_candidates(
        session,
        replay,
        start_at,
        end_at,
        head,
        tuple(coverage_entry(entry) for entry in active),
        configuration,
        read_at,
        through_recording_revision=through_recording_revision,
    )
    unique: dict[str, CoverageRecording] = {}
    for entry, value in result:
        if recording_reference(value, entry, configuration, entries) is None:
            raise DomainError("CONFLICT", "Stored closing receipt binding differs", 409)
        unique[str(recording_id(value))] = value
    return tuple(unique[key] for key in sorted(unique))


def coverage_recording_candidates(
    session: Session,
    replay: CoverageReplay,
    start_at: datetime,
    end_at: datetime,
    head: UsageLedgerHead,
    active: tuple[CoverageLedgerEntry, ...],
    configuration: ProcessingCoverageConfiguration,
    read_at: datetime,
    *,
    through_recording_revision: int | None = None,
) -> tuple[tuple[CoverageLedgerEntry, CoverageRecording], ...]:
    """Load candidate original receipts; callers must verify their entry and head bindings."""
    local = coverage_parents(replay, start_at, end_at, (), (), configuration)
    repo = ClosedRunRecordingRepository(session)
    result: list[tuple[CoverageLedgerEntry, CoverageRecording]] = []
    attempts = 0
    for run in local:
        for entry in active:
            window = entry.measurement
            if window.gpu_uuid != run.gpu_uuid or not any(
                edge.run is not None
                and edge.run.id == run.id
                and window.start_at < min(end_at, edge.end_at)
                and window.end_at > max(start_at, edge.start_at)
                for edge in replay.edges
            ):
                continue
            attempts += 1
            if attempts > 128:
                raise DomainError("USAGE_REPORT_TOO_LARGE", "Split the coverage time window", 503)
            value: CoverageRecording | None = StableWindowRecordingRepository().for_entry(
                session,
                node_id=head.node_id,
                run_id=run.id,
                entry_id=entry.id,
                trace_fingerprint=configuration.trace.fingerprint,
                recording_fingerprint=configuration.recording.fingerprint,
                read_at=read_at,
                through_recording_revision=through_recording_revision,
            )
            if value is None:
                value = repo.for_entry(
                    node_id=head.node_id,
                    run_id=run.id,
                    entry_id=entry.id,
                    trace_fingerprint=configuration.trace.fingerprint,
                    recording_fingerprint=configuration.recording.fingerprint,
                    read_at=read_at,
                    through_recording_revision=through_recording_revision,
                )
            if value is not None:
                result.append((entry, value))
    return tuple(result)


def resolve_coverage_identities(
    session: Session,
    service: NodeUserHistoryService,
    runs: tuple[ResidencyRun, ...],
    read_at: datetime,
    *,
    previous: tuple[CoverageIdentity, ...] | None = None,
    through_revision: int | None = None,
) -> tuple[CoverageIdentity, ...]:
    if previous is not None:
        if tuple(value.run for value in previous) != runs:
            raise DomainError("CONFLICT", "Coverage parent identities changed", 409)
        if previous and previous[0].identity.directory_revision is None:
            # An uninitialized directory was explicitly unknown at capture time.
            return previous
    if not runs:
        return ()
    revision = previous[0].identity.directory_revision if previous else through_revision
    answers = service.resolve_many_in_transaction(
        session,
        HistoricalNodeUserBatch(
            queries=tuple(
                HistoricalNodeUserQuery(
                    node_id=run.first.node_id,
                    os_username=run.username,
                    start_at=run.first.compute_observed_at,
                    end_at=run.last.compute_observed_at,
                    as_of=read_at,
                    through_revision=revision,
                )
                for run in runs
            )
        ),
    )
    result = tuple(
        CoverageIdentity(run=run, identity=answer)
        for run, answer in zip(runs, answers.answers, strict=True)
    )
    if previous is not None and result != previous:
        raise DomainError("CONFLICT", "Captured coverage identity history changed", 409)
    return result
