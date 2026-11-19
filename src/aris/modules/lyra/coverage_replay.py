"""Replay every source transition, retaining uncertainty rather than skipping it."""

from dataclasses import dataclass
from datetime import datetime

from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_schemas import CoverageHazard, ProcessingCoverageConfiguration
from aris.modules.lyra.residency_state import (
    ClosedResidencyCandidate,
    ResidencyRun,
    advance_residency_formation,
)
from aris.modules.lyra.trace_consumer_schemas import (
    ConsumedTraceSource,
    StoredTraceConsumption,
)
from aris.modules.lyra.trace_state import advance_usage_trace, observation_stamp
from aris.modules.lyra.usage_intervals import GpuIntervalCandidate, ObservationReference
from aris.modules.vega.usage_archive_schemas import UsageArchivePage
from aris.modules.vega.usage_schemas import UsageObservationFact


@dataclass(frozen=True)
class CoverageEdge:
    previous: int
    current: int
    start_at: datetime
    end_at: datetime
    gpu: GpuIntervalCandidate
    run: ResidencyRun | None


@dataclass(frozen=True)
class CoverageReplay:
    observations: tuple[ObservationReference, ...]
    edges: tuple[CoverageEdge, ...]
    hazards: tuple[CoverageHazard, ...]
    invalid_times: tuple[datetime, ...]


def replay_coverage(
    record: StoredTraceConsumption,
    page: UsageArchivePage,
    anchor: UsageObservationFact | None,
    configuration: ProcessingCoverageConfiguration,
) -> CoverageReplay:
    config = configuration.trace
    if (
        record.before.configuration != config
        or page.query.agent_id != config.agent_id
        or page.query.node_id != config.node_id
        or page.query.after_seq != record.before.after_seq
        or page.query.through_seq != record.position.after_seq
        or page.state != "range_end"
        or tuple(
            ConsumedTraceSource(
                reference=item.reference,
                observation=observation_stamp(item.observation) if item.observation else None,
            )
            for item in page.items
        )
        != record.sources
    ):
        raise DomainError("CONFLICT", "Coverage sources differ from their attested record", 409)
    trace, residency = record.before.trace, record.before.residency
    if trace is not None and any(item.observation is not None for item in page.items):
        if anchor is None or observation_stamp(anchor) != trace.anchor:
            raise DomainError("CONFLICT", "Coverage source anchor is unavailable or changed", 409)
    observations: list[ObservationReference] = []
    indices: dict[tuple[str, int], int] = {}
    invalid: set[datetime] = set()
    hazards: list[CoverageHazard] = []
    edges: list[CoverageEdge] = []
    closed: list[ClosedResidencyCandidate] = []

    def reference(fact: UsageObservationFact) -> int:
        value = observation_stamp(fact).reference
        key = (str(value.message_id), value.seq)
        if key not in indices:
            indices[key] = len(observations)
            observations.append(value)
        return indices[key]

    def inventory(fact: UsageObservationFact) -> None:
        if (
            fact.gpu_quality != "ok"
            or fact.compute_quality != "ok"
            or not fact.compute_counts_consistent
            or {gpu.gpu_uuid for gpu in fact.gpus} != set(configuration.gpu_uuids)
            or any(not gpu.inventory_present for gpu in fact.gpus)
            or abs((fact.gpu_observed_at - fact.compute_observed_at).total_seconds())
            > config.continuity.max_gap_seconds
        ):
            invalid.add(fact.compute_observed_at)

    if anchor is not None:
        reference(anchor)
        inventory(anchor)
    for item in page.items:
        fact = item.observation
        if fact is None:
            continue
        reference(fact)
        inventory(fact)
        old_anchor = anchor
        step = advance_usage_trace(trace, fact, rule=config.continuity, anchor=anchor)
        formed = advance_residency_formation(
            residency, step, continuity=config.continuity, formation=config.formation
        )
        closed.extend(formed.closed)
        assessment = step.assessment
        if assessment is not None and assessment.state != "same_observation":
            a, b = assessment.previous.compute_observed_at, assessment.current.compute_observed_at
            if assessment.state == "unavailable":
                if a != b:
                    hazards.append(
                        CoverageHazard(
                            start_at=min(a, b), end_at=max(a, b), reason=assessment.reason
                        )
                    )
                if assessment.reason in (
                    "CLOCK_ROLLBACK",
                    "OBSERVATION_CHANGED_AT_SAME_TIME",
                    "SOURCE_CONTENT_CONFLICT",
                ):
                    invalid.update((a, b))
            if a < b:
                if old_anchor is None:
                    raise DomainError("CONFLICT", "Coverage advancing source has no anchor", 409)
                previous, current = reference(old_anchor), reference(fact)
                candidates = {gpu.gpu_uuid: gpu for gpu in assessment.gpus}
                runs = {run.gpu_uuid: run for run in formed.checkpoint.open_runs}
                for gpu_uuid in configuration.gpu_uuids:
                    gpu = candidates.get(gpu_uuid)
                    if gpu is None:
                        gpu = GpuIntervalCandidate(
                            gpu_uuid=gpu_uuid, state="needs_review", reason="GPU_INVENTORY_CHANGED"
                        )
                    edges.append(
                        CoverageEdge(
                            previous,
                            current,
                            a,
                            b,
                            gpu,
                            runs.get(gpu_uuid) if gpu.state == "stable_owner" else None,
                        )
                    )
        trace, residency = step.checkpoint, formed.checkpoint
        if trace.anchor.reference.message_id == fact.message_id:
            anchor = fact
    if (
        trace != record.after_trace
        or residency != record.after_residency
        or tuple(closed) != record.closed_runs
    ):
        raise DomainError("CONFLICT", "Coverage replay differs from its committed trace", 409)
    return CoverageReplay(tuple(observations), tuple(edges), tuple(hazards), tuple(sorted(invalid)))
