"""Immutable candidate derivations are evidence, never a posted usage charge."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.reservation_links import (
    ExplicitReservationReference,
    ResidencyReservationAnalysis,
)
from aris.modules.lyra.residency_sessions import ResidencyFormationRule
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_schemas import UsageObservationQuery

MAX_DERIVATION_INPUT_BYTES = 262144
MAX_DERIVATION_RESULT_BYTES = 4194304


class UsageDerivationContext(IntervalContract):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    actor: Literal["system:lyra"] = "system:lyra"


class UsageDerivationRequest(IntervalContract):
    id: UUID
    sources: tuple[UsageObservationQuery, ...] = Field(min_length=1, max_length=1024)
    as_of: AwareDatetime
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    continuity: UsageContinuityRule
    formation: ResidencyFormationRule
    references: tuple[ExplicitReservationReference, ...] = Field(default=(), max_length=128)

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("sources")
    @classmethod
    def canonical_sources(
        cls, values: tuple[UsageObservationQuery, ...]
    ) -> tuple[UsageObservationQuery, ...]:
        scopes = {(item.agent_id, item.node_id) for item in values}
        if len(scopes) != 1:
            raise ValueError("a derivation requires one original source binding")
        by_id: dict[UUID, UsageObservationQuery] = {}
        by_seq: dict[int, UUID] = {}
        for item in values:
            if item.message_id in by_id and by_id[item.message_id] != item:
                raise ValueError("one original message has conflicting source references")
            if item.expected_seq in by_seq and by_seq[item.expected_seq] != item.message_id:
                raise ValueError("one original sequence has conflicting source messages")
            by_id[item.message_id] = item
            by_seq[item.expected_seq] = item.message_id
        return tuple(sorted(by_id.values(), key=lambda item: item.expected_seq))

    @field_validator("references")
    @classmethod
    def canonical_references(
        cls, values: tuple[ExplicitReservationReference, ...]
    ) -> tuple[ExplicitReservationReference, ...]:
        keys = {(item.session_index, item.reservation_id) for item in values}
        if len(keys) != len(values):
            raise ValueError("duplicate explicit reservation references")
        return tuple(
            sorted(values, key=lambda item: (item.session_index, str(item.reservation_id)))
        )


class DerivationSource(IntervalContract):
    query: UsageObservationQuery
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime
    received_at: AwareDatetime
    compute_observed_at: AwareDatetime


class UsageDerivationArtifact(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    request: UsageDerivationRequest = Field(repr=False)
    sources: tuple[DerivationSource, ...] = Field(min_length=1, max_length=1024, repr=False)
    analysis: ResidencyReservationAnalysis = Field(repr=False)

    @model_validator(mode="after")
    def original_sources_and_rules(self) -> "UsageDerivationArtifact":
        request = self.request
        attribution = self.analysis.request.attribution
        residency = attribution.residency
        if (
            tuple(item.query for item in self.sources) != request.sources
            or attribution.as_of != request.as_of
            or residency.continuity != request.continuity
            or residency.formation != request.formation
            or self.analysis.request.references != request.references
            or attribution.directory_revision not in (None, request.identity_revision)
            or any(
                item.identity.query.through_revision != request.identity_revision
                for item in attribution.attributions
            )
            or residency.trace.input_messages != len(self.sources)
            or residency.trace.unique_messages != len(self.sources)
            or residency.trace.duplicate_messages != 0
            or residency.trace.first_seq != request.sources[0].expected_seq
            or residency.trace.last_seq != request.sources[-1].expected_seq
            or (residency.trace.agent_id, residency.trace.node_id)
            != (request.sources[0].agent_id, request.sources[0].node_id)
            or any(
                max(item.observed_at, item.received_at) > request.as_of
                or item.compute_observed_at > item.observed_at
                for item in self.sources
            )
        ):
            raise ValueError("stored derivation differs from its exact sources, rules or cutoff")
        originals = {item.query.message_id: item for item in self.sources}
        for assessment in residency.trace.assessments:
            for reference in (assessment.previous, assessment.current):
                source = originals.get(reference.message_id)
                if source is None or (
                    reference.agent_id,
                    reference.node_id,
                    reference.seq,
                    reference.fingerprint,
                    reference.compute_observed_at,
                ) != (
                    source.query.agent_id,
                    source.query.node_id,
                    source.query.expected_seq,
                    source.fingerprint,
                    source.compute_observed_at,
                ):
                    raise ValueError("derived interval differs from its original source reference")
        return self


class UsageDerivationReceipt(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    id: UUID
    event_id: UUID
    duplicate: bool = Field(strict=True)
    created_at: AwareDatetime
    source_count: int = Field(ge=1, le=1024, strict=True)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class StoredUsageDerivation(IntervalContract):
    receipt: UsageDerivationReceipt
    artifact: UsageDerivationArtifact = Field(repr=False)


def canonical_derivation(value: IntervalContract) -> str:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def derivation_fingerprint(value: str, *, result: bool) -> str:
    frame = b"aris.lyra.derivation.result\x00" if result else b"aris.lyra.derivation.input\x00"
    return hashlib.sha256(frame + value.encode()).hexdigest()
