"""Bounded offline trace reconstruction; no persistent cursor, attribution or charges."""

from collections.abc import Sequence

from pydantic import Field

from aris.core.contracts import DomainError
from aris.modules.lyra.usage_intervals import (
    IntervalContract,
    ObservationReference,
    UsagePairAssessment,
    assess_usage_pair,
)
from aris.modules.vega.usage_schemas import UsageObservationFact

MAX_TRACE_MESSAGES = 1024


class UsageTraceAnalysis(IntervalContract):
    agent_id: str
    node_id: str
    first_seq: int = Field(gt=0, strict=True)
    last_seq: int = Field(gt=0, strict=True)
    input_messages: int = Field(ge=1, le=MAX_TRACE_MESSAGES, strict=True)
    unique_messages: int = Field(ge=1, le=MAX_TRACE_MESSAGES, strict=True)
    duplicate_messages: int = Field(ge=0, le=MAX_TRACE_MESSAGES, strict=True)
    assessments: tuple[UsagePairAssessment, ...] = Field(max_length=MAX_TRACE_MESSAGES - 1)


def _quarantine(
    previous: UsageObservationFact, current: UsageObservationFact, *, rollback: bool
) -> UsagePairAssessment:
    def reference(fact: UsageObservationFact) -> ObservationReference:
        return ObservationReference(
            agent_id=fact.agent_id,
            node_id=fact.node_id,
            message_id=fact.message_id,
            seq=fact.seq,
            fingerprint=fact.source_fingerprint,
            compute_observed_at=fact.compute_observed_at,
        )

    return UsagePairAssessment(
        previous=reference(previous),
        current=reference(current),
        state="unavailable",
        reason="CLOCK_ROLLBACK" if rollback else "SOURCE_CONTENT_CONFLICT",
    )


def analyze_usage_trace(
    facts: Sequence[UsageObservationFact], *, max_gap_seconds: int
) -> UsageTraceAnalysis:
    if not 1 <= len(facts) <= MAX_TRACE_MESSAGES:
        raise DomainError(
            "VALIDATION_ERROR", "An explicit bounded nonempty usage trace is required", 422
        )
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 3600:
        raise DomainError(
            "VALIDATION_ERROR", "An explicit bounded continuity interval is required", 422
        )
    unique: dict[str, UsageObservationFact] = {}
    sequences: dict[int, str] = {}
    scope = (facts[0].agent_id, facts[0].node_id)
    for fact in facts:
        if (fact.agent_id, fact.node_id) != scope:
            raise DomainError("VALIDATION_ERROR", "Usage trace must have one source binding", 422)
        key = str(fact.message_id)
        if key in unique and unique[key] != fact:
            raise DomainError("CONFLICT", "Usage source message changed within the trace", 409)
        if fact.seq in sequences and sequences[fact.seq] != key:
            raise DomainError("CONFLICT", "Usage source sequence has conflicting messages", 409)
        unique[key] = fact
        sequences[fact.seq] = key
    ordered = sorted(unique.values(), key=lambda fact: fact.seq)
    anchor = ordered[0]
    blocked_at = None
    assessments = []
    for current in ordered[1:]:
        if blocked_at is not None:
            assessments.append(
                _quarantine(anchor, current, rollback=current.compute_observed_at < blocked_at)
            )
            if current.compute_observed_at > blocked_at:
                anchor = current
                blocked_at = None
            continue
        result = assess_usage_pair(anchor, current, max_gap_seconds=max_gap_seconds)
        assessments.append(result)
        if result.state == "same_observation":
            # Keep the first source of this compute observation, including its
            # contemporaneous inventory, rather than advancing to cached copies.
            continue
        if result.reason in {"CLOCK_ROLLBACK", "OBSERVATION_CHANGED_AT_SAME_TIME"}:
            blocked_at = anchor.compute_observed_at
            continue
        anchor = current
    return UsageTraceAnalysis(
        agent_id=scope[0],
        node_id=scope[1],
        first_seq=ordered[0].seq,
        last_seq=ordered[-1].seq,
        input_messages=len(facts),
        unique_messages=len(ordered),
        duplicate_messages=len(facts) - len(ordered),
        assessments=tuple(assessments),
    )
