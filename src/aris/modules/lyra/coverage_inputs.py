"""Resolve bounded closed-receipt and historical identity inputs for source coverage."""

from datetime import datetime

from sqlalchemy.orm import Session

from aris.core.access.history_schemas import HistoricalNodeUserBatch, HistoricalNodeUserQuery
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_replay import CoverageReplay
from aris.modules.lyra.coverage_schemas import CoverageIdentity, ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_schemas import UsageLedgerEntry, UsageLedgerHead
from aris.modules.lyra.processing_coverage import (
    CoverageRecording,
    recording_parent,
    recording_reference,
)
from aris.modules.lyra.recorded_usage import _active_prefix
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository


def coverage_parents(
    replay: CoverageReplay,
    start_at: datetime,
    end_at: datetime,
    recordings: tuple[CoverageRecording, ...],
    entries: tuple[UsageLedgerEntry, ...],
    configuration: ProcessingCoverageConfiguration,
) -> tuple[ResidencyRun, ...]:
    runs: dict[str, ResidencyRun] = {}
    for edge in replay.edges:
        if edge.run is not None and edge.start_at < end_at and edge.end_at > start_at:
            previous = runs.get(edge.run.id)
            if previous is None or edge.run.intervals > previous.intervals:
                runs[edge.run.id] = edge.run
    for recording in recordings:
        parent = recording_parent(recording)
        local = runs.get(parent.id)
        if local is None:
            raise DomainError("CONFLICT", "Coverage receipt has no relevant source residency", 409)
        if not any(
            recording_reference(recording, entry, configuration, entries)
            for entry in entries
            if entry.id in recording.ledger.entries
        ):
            raise DomainError("CONFLICT", "Coverage receipt does not bind the captured ledger", 409)
        if (
            parent.first != local.first
            or parent.last.seq < local.last.seq
            or parent.intervals < local.intervals
            or (parent.intervals == local.intervals and parent != local)
        ):
            raise DomainError("CONFLICT", "Coverage closed parent differs from its source run", 409)
        runs[parent.id] = parent
    if len(runs) > 128:
        raise DomainError("USAGE_REPORT_TOO_LARGE", "Split the coverage time window", 503)
    return tuple(runs[key] for key in sorted(runs))


def find_coverage_recordings(
    session: Session,
    replay: CoverageReplay,
    start_at: datetime,
    end_at: datetime,
    head: UsageLedgerHead,
    entries: tuple[UsageLedgerEntry, ...],
    configuration: ProcessingCoverageConfiguration,
    read_at: datetime,
) -> tuple[CoverageRecording, ...]:
    active = _active_prefix(head, entries, read_at)
    local = coverage_parents(replay, start_at, end_at, (), entries, configuration)
    repo = ClosedRunRecordingRepository(session)
    result: dict[str, CoverageRecording] = {}
    attempts = 0
    for run in local:
        for entry in active:
            window = entry.details.measurement
            if (
                window.gpu_uuid != run.gpu_uuid
                or window.start_at >= min(end_at, run.last.compute_observed_at)
                or window.end_at <= max(start_at, run.first.compute_observed_at)
            ):
                continue
            attempts += 1
            if attempts > 128:
                raise DomainError("USAGE_REPORT_TOO_LARGE", "Split the coverage time window", 503)
            value = repo.for_entry(
                node_id=head.node_id,
                run_id=run.id,
                entry_id=entry.id,
                trace_fingerprint=configuration.trace.fingerprint,
                recording_fingerprint=configuration.recording.fingerprint,
                read_at=read_at,
            )
            if value is not None:
                if recording_reference(value, entry, configuration, entries) is None:
                    raise DomainError("CONFLICT", "Stored closing receipt binding differs", 409)
                result[str(value.request.id)] = value
    return tuple(result[key] for key in sorted(result))


def resolve_coverage_identities(
    session: Session,
    service: NodeUserHistoryService,
    runs: tuple[ResidencyRun, ...],
    read_at: datetime,
    *,
    previous: tuple[CoverageIdentity, ...] | None = None,
) -> tuple[CoverageIdentity, ...]:
    if previous is not None:
        if tuple(value.run for value in previous) != runs:
            raise DomainError("CONFLICT", "Coverage parent identities changed", 409)
        if previous and previous[0].identity.directory_revision is None:
            # An uninitialized directory was explicitly unknown at capture time.
            return previous
    if not runs:
        return ()
    revision = previous[0].identity.directory_revision if previous else None
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
