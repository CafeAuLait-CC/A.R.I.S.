"""Bounded open residency summaries; neither page boundaries nor restarts end use."""

import hashlib
import json
from datetime import timedelta
from typing import Literal

from pydantic import Field, field_validator, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_sessions import (
    ResidencyEvidence,
    ResidencyFormationRule,
    residency_fingerprint,
)
from aris.modules.lyra.trace_state import ObservationStamp, UsageContinuityRule, UsageTraceStep
from aris.modules.lyra.usage_intervals import IntervalContract, ObservationReference

MICROSECOND = timedelta(microseconds=1)
MAX_OPEN_RESIDENCIES = 128
RunBoundary = Literal[
    "trace_end", "unavailable", "idle_evidence", "review_boundary", "source_changed"
]


def _digest(kind: str, value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(
        b"aris.lyra.residency.run." + kind.encode() + b"\x00" + raw.encode()
    ).hexdigest()


class ResidencyRun(IntervalContract):
    candidate_only: Literal[True] = True
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    rule_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    gpu_uuid: str = Field(min_length=1, max_length=128)
    uid: int = Field(ge=0, le=4294967295, strict=True)
    username: str = Field(min_length=1, max_length=128, repr=False)
    model: str = Field(min_length=1, max_length=256)
    first: ObservationReference
    last: ObservationReference
    intervals: int = Field(ge=1, le=9223372036854775807, strict=True)
    observed_microseconds: int = Field(gt=0, le=9223372036854775807, strict=True)
    evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounded_run(self) -> "ResidencyRun":
        if (
            (self.first.agent_id, self.first.node_id) != (self.last.agent_id, self.last.node_id)
            or self.last.seq <= self.first.seq
            or self.intervals > self.last.seq - self.first.seq
            or self.last.compute_observed_at <= self.first.compute_observed_at
            or self.observed_microseconds
            != (self.last.compute_observed_at - self.first.compute_observed_at) // MICROSECOND
            or self.id != run_identity(self)
            or (
                self.intervals == 1
                and self.evidence_fingerprint
                != _digest(
                    "first_evidence",
                    {
                        "id": self.id,
                        "edge": ResidencyEvidence(
                            previous=self.first, current=self.last
                        ).model_dump(mode="json"),
                    },
                )
            )
        ):
            raise ValueError("residency run differs from its fixed identity and observed span")
        return self


def run_identity(run: ResidencyRun) -> str:
    return _digest(
        "identity",
        {
            "rule_fingerprint": run.rule_fingerprint,
            "gpu_uuid": run.gpu_uuid,
            "uid": run.uid,
            "username": run.username,
            "model": run.model,
            "first": run.first.model_dump(mode="json"),
        },
    )


class ClosedResidencyCandidate(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    run: ResidencyRun
    state: Literal["stable_candidate", "insufficient_evidence"]
    end_boundary: RunBoundary
    boundary_reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)

    @model_validator(mode="after")
    def explicit_candidate(self) -> "ClosedResidencyCandidate":
        enough = (
            self.run.intervals >= self.formation.minimum_intervals
            and self.run.observed_microseconds >= self.formation.minimum_duration_seconds * 1000000
        )
        if (
            self.run.rule_fingerprint != residency_fingerprint(self.continuity, self.formation)
            or (self.state == "stable_candidate") != enough
        ):
            raise ValueError("residency candidate differs from its explicit formation rules")
        return self


class ResidencyFormationCheckpoint(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    rule_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    last: ObservationStamp
    open_runs: tuple[ResidencyRun, ...] = Field(default=(), max_length=MAX_OPEN_RESIDENCIES)

    @field_validator("open_runs")
    @classmethod
    def ordered_targets(cls, runs: tuple[ResidencyRun, ...]) -> tuple[ResidencyRun, ...]:
        if len({run.gpu_uuid for run in runs}) != len(runs):
            raise ValueError("open residency targets must be distinct")
        return tuple(sorted(runs, key=lambda run: run.gpu_uuid))

    @model_validator(mode="after")
    def original_rules_and_scope(self) -> "ResidencyFormationCheckpoint":
        if self.rule_fingerprint != residency_fingerprint(self.continuity, self.formation) or any(
            run.rule_fingerprint != self.rule_fingerprint
            or (run.last.agent_id, run.last.node_id)
            != (self.last.reference.agent_id, self.last.reference.node_id)
            or run.last.seq > self.last.reference.seq
            or run.last.compute_observed_at != self.last.reference.compute_observed_at
            for run in self.open_runs
        ):
            raise ValueError("residency state differs from its rules or source progress")
        return self


class ResidencyFormationStep(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    checkpoint: ResidencyFormationCheckpoint
    closed: tuple[ClosedResidencyCandidate, ...] = Field(
        default=(), max_length=MAX_OPEN_RESIDENCIES
    )


def _closed(
    run: ResidencyRun,
    continuity: UsageContinuityRule,
    formation: ResidencyFormationRule,
    boundary: RunBoundary,
    reason: str,
) -> ClosedResidencyCandidate:
    enough = (
        run.intervals >= formation.minimum_intervals
        and run.observed_microseconds >= formation.minimum_duration_seconds * 1000000
    )
    return ClosedResidencyCandidate(
        continuity=continuity,
        formation=formation,
        run=run,
        state="stable_candidate" if enough else "insufficient_evidence",
        end_boundary=boundary,
        boundary_reason=reason,
    )


def preview_open_residencies(
    checkpoint: ResidencyFormationCheckpoint,
) -> tuple[ClosedResidencyCandidate, ...]:
    """A bounded trace-end preview, not evidence of the process having stopped."""
    checkpoint = ResidencyFormationCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
    return tuple(
        _closed(run, checkpoint.continuity, checkpoint.formation, "trace_end", "TRACE_END")
        for run in checkpoint.open_runs
    )


def advance_residency_formation(
    checkpoint: ResidencyFormationCheckpoint | None,
    step: UsageTraceStep,
    *,
    continuity: UsageContinuityRule,
    formation: ResidencyFormationRule,
) -> ResidencyFormationStep:
    step = UsageTraceStep.model_validate(step.model_dump(mode="json"))
    continuity = UsageContinuityRule.model_validate(continuity.model_dump(mode="json"))
    formation = ResidencyFormationRule.model_validate(formation.model_dump(mode="json"))
    fingerprint = residency_fingerprint(continuity, formation)
    if step.checkpoint.rule != continuity:
        raise DomainError("CONFLICT", "Residency and trace continuity rules differ", 409)
    if step.requires_rebuild:
        raise DomainError("CONFLICT", "Late residency source requires reconstruction", 409)
    if checkpoint is None:
        if step.kind != "initialized":
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Original residency state is required", 503)
        checkpoint = ResidencyFormationCheckpoint(
            continuity=continuity,
            formation=formation,
            rule_fingerprint=fingerprint,
            last=step.checkpoint.last,
        )
        return ResidencyFormationStep(checkpoint=checkpoint)
    checkpoint = ResidencyFormationCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
    if fingerprint != checkpoint.rule_fingerprint or (
        checkpoint.last.reference.agent_id,
        checkpoint.last.reference.node_id,
    ) != (step.incoming.agent_id, step.incoming.node_id):
        raise DomainError("CONFLICT", "Residency rules or source binding changed", 409)
    if step.kind == "replayed":
        if step.checkpoint.last != checkpoint.last:
            raise DomainError("CONFLICT", "Replayed trace differs from residency progress", 409)
        return ResidencyFormationStep(checkpoint=checkpoint)
    assessment = step.assessment
    if (
        step.kind != "advanced"
        or assessment is None
        or assessment.current != step.incoming
        or step.incoming.seq <= checkpoint.last.reference.seq
        or assessment.previous.seq > checkpoint.last.reference.seq
    ):
        raise DomainError("CONFLICT", "Residency source order differs from its checkpoint", 409)
    active = {run.gpu_uuid: run for run in checkpoint.open_runs}
    closed = []

    def finish(gpu_uuid: str, boundary: RunBoundary, reason: str) -> None:
        run = active.pop(gpu_uuid, None)
        if run is not None:
            closed.append(_closed(run, continuity, formation, boundary, reason))

    if assessment.state == "unavailable":
        for gpu_uuid in tuple(active):
            finish(gpu_uuid, "unavailable", assessment.reason)
    elif assessment.state == "interval":
        present = {gpu.gpu_uuid for gpu in assessment.gpus}
        for missing in set(active) - present:
            finish(missing, "source_changed", "GPU_DISAPPEARED")
        edge = ResidencyEvidence(previous=assessment.previous, current=assessment.current)
        edge_json = edge.model_dump(mode="json")
        assert assessment.duration_microseconds is not None
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
                or run.last != assessment.previous
            ):
                finish(gpu.gpu_uuid, "source_changed", "NONCONTIGUOUS_EVIDENCE")
                run = None
            if run is None:
                unchecked = ResidencyRun.model_construct(
                    id="0" * 64,
                    rule_fingerprint=fingerprint,
                    gpu_uuid=gpu.gpu_uuid,
                    uid=gpu.uid,
                    username=gpu.username,
                    model=gpu.model,
                    first=assessment.previous,
                    last=assessment.current,
                    intervals=1,
                    observed_microseconds=assessment.duration_microseconds,
                    evidence_fingerprint="0" * 64,
                )
                identity = run_identity(unchecked)
                run = ResidencyRun.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "id": identity,
                        "evidence_fingerprint": _digest(
                            "first_evidence", {"id": identity, "edge": edge_json}
                        ),
                    }
                )
            else:
                run = ResidencyRun.model_validate(
                    {
                        **run.model_dump(mode="json"),
                        "last": assessment.current.model_dump(mode="json"),
                        "intervals": run.intervals + 1,
                        "observed_microseconds": (
                            assessment.current.compute_observed_at - run.first.compute_observed_at
                        )
                        // MICROSECOND,
                        "evidence_fingerprint": _digest(
                            "next_evidence",
                            {"previous": run.evidence_fingerprint, "edge": edge_json},
                        ),
                    }
                )
            active[gpu.gpu_uuid] = run
    updated = ResidencyFormationCheckpoint(
        continuity=continuity,
        formation=formation,
        rule_fingerprint=fingerprint,
        last=step.checkpoint.last,
        open_runs=tuple(active.values()),
    )
    return ResidencyFormationStep(
        checkpoint=updated,
        closed=tuple(
            sorted(
                closed, key=lambda item: (item.run.first.seq, item.run.gpu_uuid, item.run.last.seq)
            )
        ),
    )
