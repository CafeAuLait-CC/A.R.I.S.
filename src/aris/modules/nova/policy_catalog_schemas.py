from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.policy_review import PolicyCaseComparison, PolicyDifference
from aris.modules.nova.policy_schemas import GpuAllocationPolicy, PolicyContract, PolicyName
from aris.modules.nova.policy_simulation import PolicyScenario


class PolicySourceReference(PolicyContract):
    name: PolicyName
    version: int = Field(ge=1, le=2147483647, strict=True)


class RecordPolicyDraft(PolicyContract):
    source: GpuAllocationPolicy = Field(repr=False)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("a nonblank single-line reason is required")
        return value


class PolicySourceView(PolicyContract):
    catalog_entry_only: Literal[True] = True
    id: UUID
    source: GpuAllocationPolicy = Field(repr=False)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_by: UUID
    command_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=256, repr=False)
    created_at: AwareDatetime


class RecordedPolicyDraft(PolicyContract):
    catalog_entry_only: Literal[True] = True
    publication_performed: Literal[False] = False
    id: UUID
    reference: PolicySourceReference
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created: bool


class PolicySourceListQuery(PolicyContract):
    name: PolicyName
    after_version: int = Field(default=0, ge=0, le=2147483647, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


class PolicySourceSummary(PolicyContract):
    summary_only: Literal[True] = True
    catalog_entry_only: Literal[True] = True
    id: UUID
    reference: PolicySourceReference
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime


class PolicySourcePage(PolicyContract):
    name: PolicyName
    versions: tuple[PolicySourceSummary, ...] = Field(max_length=50)
    next_after_version: int | None = Field(default=None, ge=1, le=2147483647, strict=True)


class PolicySourceComparisonQuery(PolicyContract):
    before: PolicySourceReference
    after: PolicySourceReference
    expected_before_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_after_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    scenarios: tuple[PolicyScenario, ...] = Field(default=(), max_length=100, repr=False)

    @model_validator(mode="after")
    def consistent_comparison(self) -> "PolicySourceComparisonQuery":
        if self.before.name != self.after.name:
            raise ValueError("stored source comparison requires one policy name")
        if len({item.name for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("scenario names must be unique")
        return self


class StoredPolicyComparison(PolicyContract):
    comparison_only: Literal[True] = True
    before_source_id: UUID
    after_source_id: UUID
    difference: PolicyDifference
    cases: tuple[PolicyCaseComparison, ...] = Field(default=(), max_length=100)
