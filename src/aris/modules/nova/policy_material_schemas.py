"""Saved supplied examples are review material, never approval or real dependency evidence."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_schemas import PolicyContract
from aris.modules.nova.policy_simulation import PolicyScenario, PolicyScenarioResult

MAX_POLICY_MATERIAL_BYTES = 65536
ImplementationReference = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]*$")
]


class RecordPolicyReviewMaterial(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_implementation_ref: ImplementationReference
    scenarios: tuple[PolicyScenario, ...] = Field(min_length=1, max_length=8, repr=False)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("a nonblank single-line material reason is required")
        return value

    @model_validator(mode="after")
    def unique_examples(self) -> "RecordPolicyReviewMaterial":
        if len({item.name for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("review examples must have unique names")
        return self


class StoredPolicyReviewMaterial(PolicyContract):
    review_material_only: Literal[True] = True
    basis: Literal["caller_supplied_examples"] = "caller_supplied_examples"
    implementation_provenance: Literal["configured_runtime_reference"] = (
        "configured_runtime_reference"
    )
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    current_dependencies_assessed: Literal[False] = False
    id: UUID
    source_id: UUID
    request: RecordPolicyReviewMaterial = Field(repr=False)
    implementation_ref: ImplementationReference
    results: tuple[PolicyScenarioResult, ...] = Field(min_length=1, max_length=8)
    created_by: UUID
    command_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound(self) -> "StoredPolicyReviewMaterial":
        source = self.request.source
        if (
            self.request.expected_implementation_ref != self.implementation_ref
            or len(self.results) != len(self.request.scenarios)
            or any(
                (
                    result.scenario_name,
                    result.policy_name,
                    result.source_version,
                    result.source_fingerprint,
                )
                != (
                    scenario.name,
                    source.name,
                    source.version,
                    self.request.expected_source_fingerprint,
                )
                for result, scenario in zip(self.results, self.request.scenarios, strict=True)
            )
            or self.fingerprint != policy_material_fingerprint(self)
            or len(canonical_policy_material(self).encode()) > MAX_POLICY_MATERIAL_BYTES
        ):
            raise ValueError("policy review material differs from its fixed source or examples")
        return self


class PolicyReviewMaterialReceipt(PolicyContract):
    review_material_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created: bool = Field(strict=True)


class PolicyMaterialCursor(PolicyContract):
    created_at: AwareDatetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PolicyMaterialListQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: PolicyMaterialCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)


class PolicyMaterialSummary(PolicyContract):
    review_material_only: Literal[True] = True
    id: UUID
    source: PolicySourceReference
    source_id: UUID
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    implementation_ref: ImplementationReference
    scenarios: int = Field(ge=1, le=8, strict=True)
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyMaterialPage(PolicyContract):
    query: PolicyMaterialListQuery
    items: tuple[PolicyMaterialSummary, ...] = Field(max_length=16)
    next_cursor: PolicyMaterialCursor | None = None


def canonical_policy_material(value: PolicyContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def policy_material_fingerprint(value: StoredPolicyReviewMaterial) -> str:
    return hashlib.sha256(
        b"aris.nova.policy_review_material\x00"
        + canonical_policy_material(value, exclude={"fingerprint"}).encode()
    ).hexdigest()
