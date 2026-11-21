"""First accepted window evidence is historical, never a ledger or coverage receipt."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.history_schemas import HistoricalNodeUserResolution
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.residency_layout_verification_schemas import (
    StableResidencyWindowReference,
    VerifiedStableResidencyWindow,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_ACCEPTED_WINDOW_BYTES = 4194304


class AcceptStableWindow(IntervalContract):
    window: StableResidencyWindowReference
    expected_recording_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)


class AcceptedStableWindowReceipt(IntervalContract):
    assessment_scope: Literal["accepted_stable_window_evidence"] = "accepted_stable_window_evidence"
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    first_request: AcceptStableWindow
    event_id: UUID
    created_at: AwareDatetime
    proof_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    duplicate: bool = Field(strict=True)


class StoredAcceptedStableWindow(IntervalContract):
    request: AcceptStableWindow
    proof: VerifiedStableResidencyWindow = Field(repr=False)
    configuration: UsagePostingConfiguration
    parent_identity: HistoricalNodeUserResolution = Field(repr=False)
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def first_acceptance_is_bound(self) -> "StoredAcceptedStableWindow":
        original, request = self.proof.original, self.request
        run, identity = original.parent_run, self.parent_identity
        query = identity.query
        if (
            self.proof.request != request.window
            or original.parent_candidate_state != "stable_candidate"
            or original.candidate.state != "stable_candidate"
            or self.configuration.fingerprint
            != request.expected_recording_configuration_fingerprint
            or self.configuration.timing_fingerprint != run.rule_fingerprint
            or run.first.compute_observed_at < self.configuration.recording_start_at
            or identity.status != "resolved"
            or identity.user_id is None
            or query.node_id != run.first.node_id
            or query.os_username != run.username
            or query.start_at != run.first.compute_observed_at
            or query.end_at != run.last.compute_observed_at
            or query.as_of != request.window.reference.source_cutoff
            or query.through_revision != request.identity_revision
            or identity.directory_revision != request.identity_revision
            or original.read_at > self.created_at
            or self.fingerprint != accepted_window_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_ACCEPTED_WINDOW_BYTES
        ):
            raise ValueError("accepted window differs from its first proof and parent identity")
        return self

    def receipt(self, *, duplicate: bool) -> AcceptedStableWindowReceipt:
        return AcceptedStableWindowReceipt(
            key=self.proof.window.key,
            first_request=self.request,
            event_id=self.event_id,
            created_at=self.created_at,
            proof_fingerprint=self.proof.fingerprint,
            fingerprint=self.fingerprint,
            duplicate=duplicate,
        )


def accepted_window_fingerprint(value: StoredAcceptedStableWindow) -> str:
    return trace_fingerprint(
        "accepted_stable_window", canonical_trace(value, exclude={"fingerprint"})
    )
