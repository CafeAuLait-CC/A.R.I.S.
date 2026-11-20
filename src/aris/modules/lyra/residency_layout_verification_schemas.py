"""Source-verified stable layout references do not assign ownership or authorize posting."""

import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_layout import (
    PlannedResidencyWindow,
    StableResidencyLayoutMethod,
    plan_residency_windows,
)
from aris.modules.lyra.residency_observed_window_schemas import VerifiedResidencyObservedWindow
from aris.modules.lyra.trace_consumer_schemas import trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract


class StableResidencyWindowReference(IntervalContract):
    reference: ResidencyRunReference
    attestation_id: UUID
    expected_attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_trace_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_method_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_index: int = Field(ge=0, le=9223372036854775806, strict=True)


class VerifiedStableResidencyWindow(IntervalContract):
    assessment_scope: Literal["source_verified_stable_residency_layout_window"] = (
        "source_verified_stable_residency_layout_window"
    )
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    platform_owner_id: None = None
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: StableResidencyWindowReference
    method: StableResidencyLayoutMethod
    layout_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    window: PlannedResidencyWindow
    original: VerifiedResidencyObservedWindow = Field(repr=False)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def binds_selected_original_window(self) -> "VerifiedStableResidencyWindow":
        request, original = self.request, self.original
        page = plan_residency_windows(
            original, self.method, first_window=request.window_index, limit=1
        )
        if (
            request.reference != original.request.reference
            or request.attestation_id != original.request.attestation_id
            or request.expected_attestation_fingerprint
            != original.request.expected_attestation_fingerprint
            or request.expected_trace_configuration_fingerprint
            != original.request.expected_trace_configuration_fingerprint
            or request.expected_method_fingerprint != self.method.fingerprint
            or len(page.windows) != 1
            or self.layout_key != page.layout_key
            or self.window != page.windows[0]
            or original.request != self.window.original
            or self.fingerprint != stable_window_fingerprint(self)
        ):
            raise ValueError("stable window differs from its configured layout and original proof")
        return self


def stable_window_fingerprint(value: VerifiedStableResidencyWindow) -> str:
    return trace_fingerprint(
        "verified_stable_residency_window",
        json.dumps(
            {
                "request": value.request.model_dump(mode="json"),
                "method": value.method.model_dump(mode="json"),
                "layout_key": value.layout_key,
                "window": value.window.model_dump(mode="json"),
                "original_fingerprint": value.original.fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
