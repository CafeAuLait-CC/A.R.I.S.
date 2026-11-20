"""Individual human source approvals, separate from publication authority."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, RecordPolicyDraft
from aris.modules.nova.policy_history_record_schemas import (
    HistoricalReviewConfiguration,
    canonical_history_record,
)
from aris.modules.nova.policy_schemas import PolicyContract

MAX_APPROVAL_BYTES = 32768


def approval_fingerprint(
    kind: str, value: PolicyContract, *, exclude: set[str] | None = None
) -> str:
    return hashlib.sha256(
        b"aris.nova.policy_approval."
        + kind.encode()
        + b"\x00"
        + canonical_history_record(value, exclude=exclude).encode()
    ).hexdigest()


class PolicyApprovalRequirements(PolicyContract):
    # Every operating choice is required. A missing configuration disables approvals.
    history: HistoricalReviewConfiguration
    allow_source_author_approval: bool = Field(strict=True)
    minimum_window_seconds: int = Field(ge=1, le=366 * 86400, strict=True)
    maximum_history_age_seconds: int = Field(ge=1, le=366 * 86400, strict=True)
    require_evaluated_quota: bool = Field(strict=True)
    validity_seconds: int = Field(ge=30, le=366 * 86400, strict=True)

    @property
    def fingerprint(self) -> str:
        return approval_fingerprint("requirements", self)


class ApprovalHistoryReference(PolicyContract):
    node_id: str = Field(min_length=1, max_length=128)
    id: UUID
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovePolicySource(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_requirements_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    history: tuple[ApprovalHistoryReference, ...] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        return RecordPolicyDraft.nonblank_reason(value)

    @model_validator(mode="after")
    def unique_sorted_nodes(self) -> "ApprovePolicySource":
        nodes = tuple(item.node_id for item in self.history)
        if nodes != tuple(sorted(set(nodes))):
            raise ValueError("approval requires one reference per node in sorted order")
        return self


class ApprovedHistoricalSample(PolicyContract):
    reference: ApprovalHistoryReference
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: UUID
    window_start_at: AwareDatetime
    window_end_at: AwareDatetime
    saved_at: AwareDatetime
    quota_state: Literal["within_limit", "exhausted", "unlimited", "not_evaluated"]
    quota_reason: str = Field(pattern=r"^[A-Z][A-Z_]+$", max_length=64)

    @field_validator("window_start_at", "window_end_at", "saved_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_window(self) -> "ApprovedHistoricalSample":
        if not self.window_start_at < self.window_end_at <= self.saved_at:
            raise ValueError("approval sample requires an original historical window")
        return self


class PolicyApprovalPreview(PolicyContract):
    individual_source_approval_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    activation_performed: Literal[False] = False
    request: ApprovePolicySource
    source_id: UUID
    source_author: UUID
    requirements: PolicyApprovalRequirements
    samples: tuple[ApprovedHistoricalSample, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bound_references(self) -> "PolicyApprovalPreview":
        if (
            self.requirements.fingerprint != self.request.expected_requirements_fingerprint
            or tuple(sample.reference for sample in self.samples) != self.request.history
        ):
            raise ValueError("approval preview differs from its exact references")
        return self


class StoredPolicyApproval(PolicyContract):
    individual_source_approval_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    activation_performed: Literal[False] = False
    id: UUID
    preview: PolicyApprovalPreview
    approved_by: UUID
    command_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("approved_at", "expires_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent(self) -> "StoredPolicyApproval":
        if (
            self.expires_at
            != self.approved_at + timedelta(seconds=self.preview.requirements.validity_seconds)
            or any(sample.saved_at > self.approved_at for sample in self.preview.samples)
            or (
                not self.preview.requirements.allow_source_author_approval
                and self.approved_by == self.preview.source_author
            )
            or approval_fingerprint("record", self, exclude={"fingerprint"}) != self.fingerprint
            or len(canonical_history_record(self).encode()) > MAX_APPROVAL_BYTES
        ):
            raise ValueError("stored approval differs from its confirmed source and requirements")
        return self


class PolicyApprovalCursor(PolicyContract):
    approved_at: AwareDatetime
    id: UUID


class PolicyApprovalListQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    before: PolicyApprovalCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)


class PolicyApprovalDetailQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    id: UUID
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyApprovalSummary(PolicyContract):
    id: UUID
    source: PolicySourceReference
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_by: UUID
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revocation_id: UUID | None = None
    revoked_at: AwareDatetime | None = None
    revocation_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent_revocation(self) -> "PolicyApprovalSummary":
        fields = (self.revocation_id, self.revoked_at, self.revocation_fingerprint)
        if any(item is None for item in fields) and not all(item is None for item in fields):
            raise ValueError("approval revocation summary must include all original references")
        return self


class PolicyApprovalPage(PolicyContract):
    individual_records_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    query: PolicyApprovalListQuery
    as_of: AwareDatetime
    items: tuple[PolicyApprovalSummary, ...] = Field(max_length=16)
    next_cursor: PolicyApprovalCursor | None = None


class PolicyApprovalReceipt(PolicyContract):
    individual_source_approval_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    activation_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: AwareDatetime
    created: bool = Field(strict=True)


class RevokePolicySourceApproval(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_id: UUID
    expected_approval_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        return RecordPolicyDraft.nonblank_reason(value)


class PolicyApprovalRevocationPreview(PolicyContract):
    revocation_only: Literal[True] = True
    publication_performed: Literal[False] = False
    request: RevokePolicySourceApproval
    approved_at: AwareDatetime
    expires_at: AwareDatetime
    existing_revocation_id: UUID | None = None


class StoredPolicyApprovalRevocation(PolicyContract):
    revocation_only: Literal[True] = True
    publication_performed: Literal[False] = False
    id: UUID
    request: RevokePolicySourceApproval
    revoked_by: UUID
    command_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    revoked_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("revoked_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def original_fingerprint(self) -> "StoredPolicyApprovalRevocation":
        if (
            approval_fingerprint("revocation", self, exclude={"fingerprint"}) != self.fingerprint
            or len(canonical_history_record(self).encode()) > 8192
        ):
            raise ValueError("approval revocation differs from its original statement")
        return self


class PolicyApprovalRevocationReceipt(PolicyContract):
    revocation_only: Literal[True] = True
    publication_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    approval_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created: bool = Field(strict=True)


class PolicyApprovalStatus(PolicyContract):
    individual_record_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    as_of: AwareDatetime
    record: StoredPolicyApproval
    revocation: StoredPolicyApprovalRevocation | None = None
    state: Literal["recorded", "expired", "revoked"]
    current_requirements_match: bool = Field(strict=True)

    @model_validator(mode="after")
    def coherent_state(self) -> "PolicyApprovalStatus":
        expected = (
            "revoked"
            if self.revocation is not None
            else "expired"
            if self.as_of >= self.record.expires_at
            else "recorded"
        )
        if expected != self.state or (
            self.revocation is not None
            and (
                self.revocation.request.approval_id != self.record.id
                or self.revocation.request.expected_approval_fingerprint != self.record.fingerprint
                or self.revocation.request.source != self.record.preview.request.source
                or self.revocation.request.expected_source_fingerprint
                != self.record.preview.request.expected_source_fingerprint
                or self.revocation.revoked_by != self.record.approved_by
                or self.revocation.revoked_at < self.record.approved_at
            )
        ):
            raise ValueError("approval status differs from its original approval and revocation")
        return self
