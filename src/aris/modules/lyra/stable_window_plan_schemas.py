"""A rolling layout cursor means durable task dispatch, never complete usage coverage."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.residency_layout import (
    ResidencyLayoutPage,
    StableResidencyLayoutMethod,
    plan_residency_windows,
)
from aris.modules.lyra.residency_observed_window_schemas import VerifiedResidencyObservedWindow
from aris.modules.lyra.stable_window_acceptance_schemas import AcceptStableWindow
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_STABLE_PLAN_BYTES = 131072
MAX_STABLE_PLAN_COUNTER = 9223372036854775807


class StableWindowPlanObservation(AcceptStableWindow):
    @model_validator(mode="after")
    def observes_from_zero(self) -> "StableWindowPlanObservation":
        if self.window.window_index != 0:
            raise ValueError("a stable layout observation starts at window zero")
        return self


class StoredStableWindowPlan(IntervalContract):
    assessment_scope: Literal["growing_stable_window_dispatch_plan"] = (
        "growing_stable_window_dispatch_plan"
    )
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    origin: StableWindowPlanObservation
    latest: StableWindowPlanObservation
    configuration: UsagePostingConfiguration
    method: StableResidencyLayoutMethod
    witness: VerifiedResidencyObservedWindow = Field(repr=False)
    version: int = Field(ge=1, le=MAX_STABLE_PLAN_COUNTER, strict=True)
    next_window: int = Field(ge=0, le=MAX_STABLE_PLAN_COUNTER, strict=True)
    event_id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def page(self) -> ResidencyLayoutPage:
        return plan_residency_windows(self.witness, self.method, first_window=0, limit=1)

    @model_validator(mode="after")
    def bound_progress(self) -> "StoredStableWindowPlan":
        original, window, run = self.witness.request, self.latest.window, self.witness.parent_run
        if (
            original.reference != window.reference
            or original.attestation_id != window.attestation_id
            or original.expected_attestation_fingerprint != window.expected_attestation_fingerprint
            or original.expected_trace_configuration_fingerprint
            != window.expected_trace_configuration_fingerprint
            or original.after_interval != 0
            or original.through_interval != 1
            or self.origin.window.reference.run_id != run.id
            or self.origin.window.expected_trace_configuration_fingerprint
            != window.expected_trace_configuration_fingerprint
            or any(
                value.expected_recording_configuration_fingerprint != self.configuration.fingerprint
                or value.window.expected_method_fingerprint != self.method.fingerprint
                for value in (self.origin, self.latest)
            )
            or self.latest.identity_revision < self.origin.identity_revision
            or window.reference.source_cutoff < self.origin.window.reference.source_cutoff
            or window.reference.source_cutoff > self.updated_at
            or run.first.compute_observed_at < self.configuration.recording_start_at
            or run.rule_fingerprint != self.configuration.timing_fingerprint
            or self.created_at > self.updated_at
            or self.witness.read_at > self.updated_at
            or self.key != self.page.layout_key
            or self.next_window > self.page.total_planned_windows
            or (self.version == 1 and (self.origin != self.latest or self.next_window != 0))
            or self.fingerprint != stable_plan_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_STABLE_PLAN_BYTES
        ):
            raise ValueError("stable dispatch progress differs from its fixed source or layout")
        return self


class StableWindowPlanReceipt(IntervalContract):
    plan: StoredStableWindowPlan
    duplicate: bool = Field(strict=True)


class StableWindowDispatchRequest(IntervalContract):
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version: int = Field(ge=1, le=MAX_STABLE_PLAN_COUNTER, strict=True)
    limit: int = Field(default=1, ge=1, le=8, strict=True)


class StableWindowDispatchReceipt(IntervalContract):
    plan: StoredStableWindowPlan
    job_ids: tuple[UUID, ...] = Field(max_length=8)


def stable_plan_fingerprint(value: StoredStableWindowPlan) -> str:
    return trace_fingerprint("stable_window_plan", canonical_trace(value, exclude={"fingerprint"}))
