from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from aris.modules.nova.policy_schemas import GpuAllocationPolicy, PolicyContract, PolicyName


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
