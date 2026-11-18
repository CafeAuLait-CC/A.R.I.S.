"""Bounded sampled-residency groups; no platform attribution, ledger or quota result."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import (
    IntervalContract,
    ObservationReference,
    UsagePairAssessment,
)
from aris.modules.lyra.usage_trace import (
    MAX_TRACE_MESSAGES,
    UsageTraceAnalysis,
    analyze_usage_trace,
)
from aris.modules.vega.usage_schemas import UsageObservationFact

MAX_SESSION_GPU_INTERVALS = 16384


class ResidencyFormationRule(IntervalContract):
    basis: Literal["compute_process_residency"] = "compute_process_residency"
    minimum_intervals: int = Field(ge=1, le=MAX_TRACE_MESSAGES - 1, strict=True)
    minimum_duration_seconds: int = Field(ge=0, le=3600, strict=True)


def residency_fingerprint(
    continuity: UsageContinuityRule, formation: ResidencyFormationRule
) -> str:
    body = json.dumps(
        {
            "continuity": continuity.model_dump(mode="json"),
            "formation": formation.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(b"aris.lyra.residency.formation\x00" + body).hexdigest()


class ResidencyEvidence(IntervalContract):
    previous: ObservationReference
    current: ObservationReference

    @model_validator(mode="after")
    def increasing_source(self) -> "ResidencyEvidence":
        a, b = self.previous, self.current
        if (
            (a.agent_id, a.node_id) != (b.agent_id, b.node_id)
            or a.seq >= b.seq
            or a.compute_observed_at >= b.compute_observed_at
        ):
            raise ValueError("residency evidence requires increasing source observations")
        return self


class ResidencySessionCandidate(IntervalContract):
    candidate_only: Literal[True] = True
    gpu_uuid: str = Field(min_length=1, max_length=128)
    uid: int = Field(ge=0, le=4294967295, strict=True)
    username: str = Field(min_length=1, max_length=128, repr=False)
    model: str = Field(min_length=1, max_length=256)
    evidence: tuple[ResidencyEvidence, ...] = Field(min_length=1, max_length=MAX_TRACE_MESSAGES - 1)
    observed_microseconds: int = Field(gt=0, strict=True)
    state: Literal["stable_candidate", "insufficient_evidence"]
    end_boundary: Literal[
        "trace_end", "unavailable", "idle_evidence", "review_boundary", "source_changed"
    ]
    boundary_reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)

    @model_validator(mode="after")
    def contiguous_evidence(self) -> "ResidencySessionCandidate":
        if any(
            a.current != b.previous for a, b in zip(self.evidence, self.evidence[1:], strict=False)
        ):
            raise ValueError("residency candidate cannot bridge different source boundaries")
        delta = (
            self.evidence[-1].current.compute_observed_at
            - self.evidence[0].previous.compute_observed_at
        )
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        if micros != self.observed_microseconds:
            raise ValueError("residency duration differs from its evidence")
        return self


class ResidencySessionAnalysis(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    rule_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    trace: UsageTraceAnalysis
    sessions: tuple[ResidencySessionCandidate, ...] = Field(max_length=MAX_SESSION_GPU_INTERVALS)

    @model_validator(mode="after")
    def complete_candidate_evidence(self) -> "ResidencySessionAnalysis":
        if self.rule_fingerprint != residency_fingerprint(self.continuity, self.formation):
            raise ValueError("residency rule fingerprint differs")
        expected = {}
        if sum(len(a.gpus) for a in self.trace.assessments) > MAX_SESSION_GPU_INTERVALS:
            raise ValueError("residency analysis exceeds its GPU interval budget")
        for assessment in self.trace.assessments:
            if any(
                (reference.agent_id, reference.node_id) != (self.trace.agent_id, self.trace.node_id)
                or not self.trace.first_seq <= reference.seq <= self.trace.last_seq
                for reference in (assessment.previous, assessment.current)
            ):
                raise ValueError("residency trace source range differs")
            if assessment.state == "interval" and (
                assessment.duration_microseconds is None
                or assessment.duration_microseconds > self.continuity.max_gap_seconds * 1000000
            ):
                raise ValueError("residency interval exceeds its continuity rule")
            for gpu in assessment.gpus:
                if gpu.state == "stable_owner":
                    key = (
                        assessment.previous.message_id,
                        assessment.current.message_id,
                        gpu.gpu_uuid,
                    )
                    expected[key] = (assessment.previous, assessment.current, gpu)
        seen = set()
        for session in self.sessions:
            enough = (
                len(session.evidence) >= self.formation.minimum_intervals
                and session.observed_microseconds
                >= self.formation.minimum_duration_seconds * 1000000
            )
            if (session.state == "stable_candidate") != enough:
                raise ValueError("residency candidate threshold differs")
            for edge in session.evidence:
                key = (edge.previous.message_id, edge.current.message_id, session.gpu_uuid)
                if key in seen or key not in expected:
                    raise ValueError(
                        "residency evidence is duplicated or not a stable owner interval"
                    )
                a, b, gpu = expected[key]
                if (edge.previous, edge.current, session.uid, session.username, session.model) != (
                    a,
                    b,
                    gpu.uid,
                    gpu.username,
                    gpu.model,
                ):
                    raise ValueError("residency owner or original source evidence differs")
                seen.add(key)
        if seen != set(expected):
            raise ValueError("stable source intervals cannot be silently omitted")
        return self


@dataclass
class _OpenResidency:
    uid: int
    username: str
    model: str
    edges: list[ResidencyEvidence]


def build_residency_sessions(
    facts: Sequence[UsageObservationFact],
    *,
    continuity: UsageContinuityRule,
    formation: ResidencyFormationRule,
) -> ResidencySessionAnalysis:
    continuity = UsageContinuityRule.model_validate(continuity.model_dump(mode="json"))
    formation = ResidencyFormationRule.model_validate(formation.model_dump(mode="json"))
    if not 1 <= len(facts) <= MAX_TRACE_MESSAGES:
        raise DomainError("VALIDATION_ERROR", "Residency analysis requires a bounded trace", 422)
    facts = [UsageObservationFact.model_validate(f.model_dump(mode="json")) for f in facts]
    trace = analyze_usage_trace(facts, max_gap_seconds=continuity.max_gap_seconds)
    if sum(len(a.gpus) for a in trace.assessments) > MAX_SESSION_GPU_INTERVALS:
        raise DomainError("VALIDATION_ERROR", "Residency GPU interval budget exceeded", 422)
    active: dict[str, _OpenResidency] = {}
    sessions = []

    def finish(
        gpu_uuid: str,
        boundary: Literal[
            "trace_end", "unavailable", "idle_evidence", "review_boundary", "source_changed"
        ],
        reason: str,
    ) -> None:
        run = active.pop(gpu_uuid, None)
        if run is None:
            return
        delta = (
            run.edges[-1].current.compute_observed_at - run.edges[0].previous.compute_observed_at
        )
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        enough = (
            len(run.edges) >= formation.minimum_intervals
            and micros >= formation.minimum_duration_seconds * 1000000
        )
        sessions.append(
            ResidencySessionCandidate(
                gpu_uuid=gpu_uuid,
                uid=run.uid,
                username=run.username,
                model=run.model,
                evidence=tuple(run.edges),
                observed_microseconds=micros,
                state="stable_candidate" if enough else "insufficient_evidence",
                end_boundary=boundary,
                boundary_reason=reason,
            )
        )

    def apply(assessment: UsagePairAssessment) -> None:
        if assessment.state == "same_observation":
            return
        if assessment.state == "unavailable":
            for gpu_uuid in list(active):
                finish(gpu_uuid, "unavailable", assessment.reason)
            return
        present = {gpu.gpu_uuid for gpu in assessment.gpus}
        for missing in set(active) - present:
            finish(missing, "source_changed", "GPU_DISAPPEARED")
        for gpu in assessment.gpus:
            if gpu.state != "stable_owner":
                finish(
                    gpu.gpu_uuid,
                    "idle_evidence" if gpu.state == "observed_idle" else "review_boundary",
                    gpu.reason,
                )
                continue
            assert gpu.uid is not None and gpu.username is not None and gpu.model is not None
            run = active.get(gpu.gpu_uuid)
            if run is not None and (
                (run.uid, run.username, run.model) != (gpu.uid, gpu.username, gpu.model)
                or run.edges[-1].current != assessment.previous
            ):
                finish(gpu.gpu_uuid, "source_changed", "NONCONTIGUOUS_EVIDENCE")
                run = None
            if run is None:
                run = _OpenResidency(gpu.uid, gpu.username, gpu.model, [])
                active[gpu.gpu_uuid] = run
            run.edges.append(
                ResidencyEvidence(previous=assessment.previous, current=assessment.current)
            )

    for assessment in trace.assessments:
        apply(assessment)
    for gpu_uuid in list(active):
        finish(gpu_uuid, "trace_end", "TRACE_END")
    sessions.sort(
        key=lambda item: (
            item.evidence[0].previous.seq,
            item.gpu_uuid,
            item.evidence[-1].current.seq,
        )
    )
    return ResidencySessionAnalysis(
        continuity=continuity,
        formation=formation,
        rule_fingerprint=residency_fingerprint(continuity, formation),
        trace=trace,
        sessions=tuple(sessions),
    )
