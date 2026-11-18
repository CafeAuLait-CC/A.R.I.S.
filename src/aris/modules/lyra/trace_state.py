"""Resumable candidate transitions; persistence, inbox dedupe and charging are separate."""

import hashlib
import json
from datetime import UTC
from typing import Any, Literal

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.usage_intervals import (
    IntervalContract,
    ObservationReference,
    UsagePairAssessment,
    assess_usage_pair,
)
from aris.modules.vega.usage_schemas import UsageObservationFact

MAX_CHECKPOINT_BYTES = 8192


class UsageContinuityRule(IntervalContract):
    basis: Literal["compute_process_residency"] = "compute_process_residency"
    max_gap_seconds: int = Field(ge=1, le=3600, strict=True)


def continuity_fingerprint(rule: UsageContinuityRule) -> str:
    validated = UsageContinuityRule.model_validate(rule.model_dump(mode="json"))
    body = json.dumps(validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"aris.lyra.continuity\x00" + body.encode()).hexdigest()


class ObservationStamp(IntervalContract):
    reference: ObservationReference
    fact_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounded_binding(self) -> "ObservationStamp":
        if any(
            not 1 <= len(value) <= 128
            for value in (self.reference.agent_id, self.reference.node_id)
        ):
            raise ValueError("observation binding is outside its size budget")
        if self.reference.seq > 9223372036854775807:
            raise ValueError("observation sequence exceeds its persisted source range")
        return self


class UsageTraceCheckpoint(IntervalContract):
    candidate_only: Literal[True] = True
    rule: UsageContinuityRule
    rule_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    anchor: ObservationStamp
    last: ObservationStamp
    blocked_at: AwareDatetime | None = None

    @field_validator("blocked_at")
    @classmethod
    def utc_time(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def coherent_state(self) -> "UsageTraceCheckpoint":
        a, b = self.anchor.reference, self.last.reference
        if self.rule_fingerprint != continuity_fingerprint(self.rule):
            raise ValueError("continuity rule fingerprint differs")
        if (a.agent_id, a.node_id) != (b.agent_id, b.node_id) or a.seq > b.seq:
            raise ValueError("checkpoint source binding or sequence differs")
        if a.seq == b.seq and self.anchor != self.last:
            raise ValueError("one source sequence cannot contain different facts")
        if a.message_id == b.message_id and self.anchor != self.last:
            raise ValueError("one source message cannot contain different facts")
        if self.blocked_at is None:
            if a.compute_observed_at != b.compute_observed_at:
                raise ValueError("an unblocked anchor differs from its compute high water")
        elif self.blocked_at != a.compute_observed_at or b.compute_observed_at > self.blocked_at:
            raise ValueError("checkpoint quarantine high water differs")
        return self


class UsageTraceStep(IntervalContract):
    candidate_only: Literal[True] = True
    kind: Literal["initialized", "advanced", "replayed", "late_requires_rebuild"]
    incoming: ObservationReference
    checkpoint: UsageTraceCheckpoint
    assessment: UsagePairAssessment | None = None
    requires_rebuild: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def coherent_step(self) -> "UsageTraceStep":
        if (self.kind == "late_requires_rebuild") != self.requires_rebuild:
            raise ValueError("late source must remain marked for reconstruction")
        if (self.kind == "advanced") != (self.assessment is not None):
            raise ValueError("only an advancing source can contain a new assessment")
        if (
            self.kind in {"initialized", "advanced"}
            and self.incoming != self.checkpoint.last.reference
        ):
            raise ValueError("advancing source differs from the checkpoint")
        return self


def _fact(value: UsageObservationFact) -> UsageObservationFact:
    try:
        return UsageObservationFact.model_validate(value.model_dump(mode="json"))
    except (ValueError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid historical observation fact", 422) from None


def observation_stamp(fact: UsageObservationFact) -> ObservationStamp:
    fact = _fact(fact)
    encoded = json.dumps(fact.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ObservationStamp(
        reference=ObservationReference(
            agent_id=fact.agent_id,
            node_id=fact.node_id,
            message_id=fact.message_id,
            seq=fact.seq,
            fingerprint=fact.source_fingerprint,
            compute_observed_at=fact.compute_observed_at,
        ),
        fact_digest=hashlib.sha256(
            b"aris.lyra.observation.fact\x00" + encoded.encode()
        ).hexdigest(),
    )


def _checkpoint(
    rule: UsageContinuityRule,
    anchor: ObservationStamp,
    last: ObservationStamp,
    blocked_at: AwareDatetime | None = None,
) -> UsageTraceCheckpoint:
    return UsageTraceCheckpoint(
        rule=rule,
        rule_fingerprint=continuity_fingerprint(rule),
        anchor=anchor,
        last=last,
        blocked_at=blocked_at,
    )


def advance_usage_trace(
    checkpoint: UsageTraceCheckpoint | None,
    current: UsageObservationFact,
    *,
    rule: UsageContinuityRule,
    anchor: UsageObservationFact | None = None,
) -> UsageTraceStep:
    rule = UsageContinuityRule.model_validate(rule.model_dump(mode="json"))
    current = _fact(current)
    incoming = observation_stamp(current)
    if checkpoint is None:
        if anchor is not None:
            raise DomainError("VALIDATION_ERROR", "Initial trace cannot contain an old anchor", 422)
        return UsageTraceStep(
            kind="initialized",
            incoming=incoming.reference,
            checkpoint=_checkpoint(rule, incoming, incoming),
        )
    try:
        checkpoint = UsageTraceCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
    except (ValueError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid candidate trace checkpoint", 422) from None
    if checkpoint.rule_fingerprint != continuity_fingerprint(rule):
        raise DomainError(
            "CONFLICT", "Trace continuity rules changed; reconstruction is required", 409
        )
    last = checkpoint.last.reference
    if (last.agent_id, last.node_id) != (current.agent_id, current.node_id):
        raise DomainError("VALIDATION_ERROR", "Trace requires the same source binding", 422)
    for known in (checkpoint.last, checkpoint.anchor):
        if current.message_id == known.reference.message_id:
            if incoming != known:
                raise DomainError("CONFLICT", "Known trace message changed", 409)
            return UsageTraceStep(
                kind="replayed",
                incoming=incoming.reference,
                checkpoint=checkpoint,
            )
        if current.seq == known.reference.seq:
            raise DomainError("CONFLICT", "Known trace sequence has another message", 409)
    if current.seq < last.seq:
        return UsageTraceStep(
            kind="late_requires_rebuild",
            incoming=incoming.reference,
            checkpoint=checkpoint,
            requires_rebuild=True,
        )
    if anchor is None:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Original trace anchor is required", 503)
    anchor = _fact(anchor)
    if observation_stamp(anchor) != checkpoint.anchor:
        raise DomainError("CONFLICT", "Original trace anchor differs from its checkpoint", 409)
    blocked = checkpoint.blocked_at
    next_anchor = incoming
    if blocked is not None:
        assessment = UsagePairAssessment(
            previous=checkpoint.anchor.reference,
            current=incoming.reference,
            state="unavailable",
            reason="CLOCK_ROLLBACK"
            if current.compute_observed_at < blocked
            else "SOURCE_CONTENT_CONFLICT",
        )
        if current.compute_observed_at > blocked:
            blocked = None
        else:
            next_anchor = checkpoint.anchor
    else:
        assessment = assess_usage_pair(anchor, current, max_gap_seconds=rule.max_gap_seconds)
        if assessment.state == "same_observation":
            next_anchor = checkpoint.anchor
        elif assessment.reason in {"CLOCK_ROLLBACK", "OBSERVATION_CHANGED_AT_SAME_TIME"}:
            blocked = anchor.compute_observed_at
            next_anchor = checkpoint.anchor
    return UsageTraceStep(
        kind="advanced",
        incoming=incoming.reference,
        assessment=assessment,
        checkpoint=_checkpoint(rule, next_anchor, incoming, blocked),
    )


def encode_trace_checkpoint(checkpoint: UsageTraceCheckpoint) -> bytes:
    try:
        validated = UsageTraceCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
        body = json.dumps(
            validated.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        if len(body) > MAX_CHECKPOINT_BYTES:
            raise ValueError("candidate checkpoint exceeds its size budget")
        return body
    except (ValueError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid candidate trace checkpoint", 422) from None


def decode_trace_checkpoint(body: bytes) -> UsageTraceCheckpoint:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate checkpoint key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("nonfinite checkpoint value")

    try:
        if not body or len(body) > MAX_CHECKPOINT_BYTES:
            raise ValueError("candidate checkpoint exceeds its size budget")
        raw = json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
        return UsageTraceCheckpoint.model_validate(raw)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid candidate trace checkpoint", 422) from None
