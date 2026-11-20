"""Human comments are source-bound records, never approval or publication."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_schemas import PolicyContract

MAX_POLICY_FEEDBACK_BYTES = 16384


class PolicyFeedbackMaterial(PolicyContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class RecordPolicyFeedback(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    material: PolicyFeedbackMaterial | None = None
    kind: Literal["comment", "changes_requested"]
    body: str = Field(min_length=1, max_length=4000, repr=False)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("body")
    @classmethod
    def plain_body(cls, value: str) -> str:
        value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not value or any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("feedback requires nonblank plain text")
        return value

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("feedback requires a nonblank single-line reason")
        return value


class StoredPolicyFeedback(PolicyContract):
    feedback_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source_id: UUID
    request: RecordPolicyFeedback = Field(repr=False)
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
    def intact(self) -> "StoredPolicyFeedback":
        if (
            self.fingerprint != policy_feedback_fingerprint(self)
            or len(canonical_policy_feedback(self).encode()) > MAX_POLICY_FEEDBACK_BYTES
        ):
            raise ValueError("policy feedback differs from its fixed request")
        return self


class PolicyFeedbackReceipt(PolicyContract):
    feedback_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created: bool = Field(strict=True)


class PolicyFeedbackCursor(PolicyContract):
    created_at: AwareDatetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PolicyFeedbackListQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: PolicyFeedbackCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)


class PolicyFeedbackPage(PolicyContract):
    query: PolicyFeedbackListQuery
    items: tuple[StoredPolicyFeedback, ...] = Field(max_length=16)
    next_cursor: PolicyFeedbackCursor | None = None


def canonical_policy_feedback(value: PolicyContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def policy_feedback_fingerprint(value: StoredPolicyFeedback) -> str:
    return hashlib.sha256(
        b"aris.nova.policy_feedback\x00"
        + canonical_policy_feedback(value, exclude={"fingerprint"}).encode()
    ).hexdigest()
