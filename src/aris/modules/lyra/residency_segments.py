"""Local sampled edges retain their parent run; they are not posted measurements."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_sessions import ResidencyFormationRule
from aris.modules.lyra.residency_state import (
    MICROSECOND,
    ClosedResidencyCandidate,
    ResidencyRun,
    advance_residency_formation,
)
from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_schemas import (
    ConsumedTraceSource,
    StoredTraceConsumption,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.trace_state import (
    UsageContinuityRule,
    advance_usage_trace,
    observation_stamp,
)
from aris.modules.lyra.usage_intervals import IntervalContract, ObservationReference
from aris.modules.vega.usage_archive_schemas import UsageArchivePage
from aris.modules.vega.usage_schemas import UsageObservationFact

MAX_RESIDENCY_SEGMENTS = 16384
MAX_SEGMENT_ANALYSIS_BYTES = 4194304


class ResidencySegmentQuery(IntervalContract):
    attestation_id: UUID
    expected_attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResidencySegment(IntervalContract):
    candidate_only: Literal[True] = True
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_at_segment_end: ResidencyRun = Field(repr=False)
    first: ObservationReference
    last: ObservationReference
    interval_offset: int = Field(ge=0, le=9223372036854775807, strict=True)
    intervals: int = Field(ge=1, le=128, strict=True)
    observed_microseconds: int = Field(gt=0, le=9223372036854775807, strict=True)
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_state_at_segment_end: Literal["stable_candidate", "insufficient_evidence"]

    @model_validator(mode="after")
    def exact_local_span(self) -> "ResidencySegment":
        run = self.run_at_segment_end
        if (
            self.last != run.last
            or (self.first.agent_id, self.first.node_id) != (run.first.agent_id, run.first.node_id)
            or not run.first.seq <= self.first.seq < self.last.seq
            or not run.first.compute_observed_at
            <= self.first.compute_observed_at
            < self.last.compute_observed_at
            or self.interval_offset + self.intervals != run.intervals
            or (self.interval_offset == 0) != (self.first == run.first)
            or self.intervals > self.last.seq - self.first.seq
            or self.observed_microseconds
            != (self.last.compute_observed_at - self.first.compute_observed_at) // MICROSECOND
            or self.observed_microseconds > run.observed_microseconds
            or self.id != segment_identity(self)
        ):
            raise ValueError("residency segment differs from its original run and sampled edges")
        return self


def segment_identity(value: ResidencySegment) -> str:
    return trace_fingerprint(
        "residency_segment_identity",
        "\n".join(
            (value.run_at_segment_end.id, canonical_trace(value.first), canonical_trace(value.last))
        ),
    )


class ResidencySegmentAnalysis(IntervalContract):
    assessment_scope: Literal["sampled_residency_segments"] = "sampled_residency_segments"
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    total_usage_microseconds: None = None
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    query: ResidencySegmentQuery
    attestation: StoredTraceAttestation
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    segments: tuple[ResidencySegment, ...] = Field(max_length=MAX_RESIDENCY_SEGMENTS, repr=False)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def explicit_scope(self) -> "ResidencySegmentAnalysis":
        from aris.modules.lyra.residency_sessions import residency_fingerprint

        proof = self.attestation
        ordered = sorted(
            self.segments,
            key=lambda item: (item.first.seq, item.run_at_segment_end.gpu_uuid, item.last.seq),
        )
        if (
            self.query.attestation_id != proof.request.id
            or self.query.expected_attestation_fingerprint != proof.fingerprint
            or proof.created_at > self.read_at
            or list(self.segments) != ordered
            or len({item.run_at_segment_end.id for item in self.segments}) != len(self.segments)
            or any(
                (item.first.agent_id, item.first.node_id) != (self.agent_id, self.node_id)
                or item.run_at_segment_end.rule_fingerprint
                != residency_fingerprint(self.continuity, self.formation)
                or not proof.first_seq <= item.last.seq <= proof.last_seq
                or (
                    (item.run_state_at_segment_end == "stable_candidate")
                    != (
                        item.run_at_segment_end.intervals >= self.formation.minimum_intervals
                        and item.run_at_segment_end.observed_microseconds
                        >= self.formation.minimum_duration_seconds * 1000000
                    )
                )
                for item in self.segments
            )
            or self.fingerprint != segment_analysis_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_SEGMENT_ANALYSIS_BYTES
        ):
            raise ValueError("residency segment analysis differs from its fixed source proof")
        return self


def segment_analysis_fingerprint(value: ResidencySegmentAnalysis) -> str:
    return trace_fingerprint(
        "residency_segments", canonical_trace(value, exclude={"fingerprint", "read_at"})
    )


def rebuild_residency_segments(
    record: StoredTraceConsumption,
    page: UsageArchivePage,
    anchor: UsageObservationFact | None,
) -> tuple[ResidencySegment, ...]:
    """Pure local replay; the caller establishes the starting prefix's attestation."""
    record = StoredTraceConsumption.model_validate(record.model_dump(mode="json"))
    page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
    config, before = record.before.configuration, record.before
    if (
        page.query.agent_id != config.agent_id
        or page.query.node_id != config.node_id
        or page.query.after_seq != before.after_seq
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
        raise DomainError("CONFLICT", "Segment sources differ from their fixed trace record", 409)
    trace, residency = before.trace, before.residency
    if trace is not None and any(item.observation is not None for item in page.items):
        if anchor is None or observation_stamp(anchor) != trace.anchor:
            raise DomainError("CONFLICT", "Original segment anchor is unavailable or changed", 409)
    segments: dict[str, ResidencySegment] = {}
    closed: list[ClosedResidencyCandidate] = []
    for item in page.items:
        fact = item.observation
        if fact is None:
            continue
        step = advance_usage_trace(trace, fact, rule=config.continuity, anchor=anchor)
        formed = advance_residency_formation(
            residency, step, continuity=config.continuity, formation=config.formation
        )
        closed.extend(formed.closed)
        assessment = step.assessment
        if assessment is not None and assessment.state == "interval":
            current_runs = {run.gpu_uuid: run for run in formed.checkpoint.open_runs}
            for gpu in assessment.gpus:
                if gpu.state != "stable_owner":
                    continue
                run = current_runs[gpu.gpu_uuid]
                previous = segments.get(run.id)
                if previous is not None and (
                    previous.last != assessment.previous
                    or previous.run_at_segment_end.intervals + 1 != run.intervals
                ):
                    raise DomainError(
                        "CONFLICT", "Local residency segment edges do not adjoin", 409
                    )
                first = previous.first if previous else assessment.previous
                enough = (
                    run.intervals >= config.formation.minimum_intervals
                    and run.observed_microseconds
                    >= config.formation.minimum_duration_seconds * 1000000
                )
                unchecked = ResidencySegment.model_construct(
                    id="0" * 64,
                    run_at_segment_end=run,
                    first=first,
                    last=assessment.current,
                    interval_offset=previous.interval_offset if previous else run.intervals - 1,
                    intervals=previous.intervals + 1 if previous else 1,
                    observed_microseconds=(
                        assessment.current.compute_observed_at - first.compute_observed_at
                    )
                    // MICROSECOND,
                    evidence_fingerprint=trace_fingerprint(
                        "residency_segment_edge",
                        "\n".join(
                            (
                                previous.evidence_fingerprint if previous else run.id,
                                canonical_trace(assessment.previous),
                                canonical_trace(assessment.current),
                            )
                        ),
                    ),
                    run_state_at_segment_end="stable_candidate"
                    if enough
                    else "insufficient_evidence",
                )
                segments[run.id] = ResidencySegment.model_validate(
                    {**unchecked.model_dump(mode="json"), "id": segment_identity(unchecked)}
                )
                if len(segments) > MAX_RESIDENCY_SEGMENTS:
                    raise DomainError(
                        "USAGE_EVIDENCE_TOO_LARGE",
                        "Residency segment result exceeds its budget",
                        503,
                    )
        trace, residency = step.checkpoint, formed.checkpoint
        if trace.anchor.reference.message_id == fact.message_id:
            anchor = fact
    if (
        trace != record.after_trace
        or residency != record.after_residency
        or tuple(closed) != record.closed_runs
    ):
        raise DomainError(
            "CONFLICT", "Segment replay differs from its fixed trace transitions", 409
        )
    return tuple(
        sorted(
            segments.values(),
            key=lambda value: (value.first.seq, value.run_at_segment_end.gpu_uuid, value.last.seq),
        )
    )
