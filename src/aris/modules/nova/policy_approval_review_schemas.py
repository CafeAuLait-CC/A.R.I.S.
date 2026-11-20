"""A bounded count of current human endorsements, never a publication ticket."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.admin_schemas import IdentityNamespace
from aris.modules.nova.policy_approval_schemas import (
    PolicyApprovalRequirements,
    PolicyApprovalSummary,
    approval_fingerprint,
)
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_schemas import PolicyContract

APPROVAL_REVIEW_LIMIT = 128


class PolicyApprovalReviewRequirements(PolicyContract):
    namespace: IdentityNamespace
    approval: PolicyApprovalRequirements
    minimum_distinct_approvers: int = Field(ge=1, le=16, strict=True)
    signer_selection: Literal["latest_recorded_statement"]

    @model_validator(mode="after")
    def explicit_namespace(self) -> "PolicyApprovalReviewRequirements":
        if self.namespace.provider != "slack":
            raise ValueError("approval review requires an explicit supported workspace")
        return self

    @property
    def fingerprint(self) -> str:
        return approval_fingerprint("review_requirements", self)


class PolicyApprovalReviewQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_review_requirements_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyApprovalReviewExport(PolicyApprovalReviewQuery):
    expected_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovalReviewEntry(PolicyContract):
    approval: PolicyApprovalSummary
    approver_label: str | None = Field(default=None, min_length=1, max_length=128)
    state: Literal[
        "counted",
        "superseded",
        "revoked",
        "expired",
        "different_requirements",
        "approver_unavailable",
    ]


class PolicyApprovalReview(PolicyContract):
    review_only: Literal[True] = True
    publication_authorized: Literal[False] = False
    current_runtime_dependencies_assessed: Literal[False] = False
    query: PolicyApprovalReviewQuery
    source_id: UUID
    requirements: PolicyApprovalReviewRequirements
    as_of: AwareDatetime
    candidate_limit: Literal[128] = 128
    truncated: bool = Field(strict=True)
    entries: tuple[ApprovalReviewEntry, ...] = Field(max_length=APPROVAL_REVIEW_LIMIT)
    qualifying_signers: int = Field(ge=0, le=APPROVAL_REVIEW_LIMIT, strict=True)
    outcome: Literal["minimum_met", "minimum_not_met", "unknown"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def consistent_count(self) -> "PolicyApprovalReview":
        counted = tuple(item for item in self.entries if item.state == "counted")
        count = len(counted)
        expected = (
            "minimum_met"
            if count >= self.requirements.minimum_distinct_approvers
            else "unknown"
            if self.truncated
            else "minimum_not_met"
        )
        if (
            self.query.expected_review_requirements_fingerprint != self.requirements.fingerprint
            or self.qualifying_signers != count
            or len({item.approval.approved_by for item in counted}) != count
            or len({item.approval.id for item in self.entries}) != len(self.entries)
            or self.outcome != expected
            or any(
                (item.approval.source, item.approval.source_fingerprint)
                != (self.query.source, self.query.expected_source_fingerprint)
                for item in self.entries
            )
            or any(
                item.approval.revocation_id is not None
                or item.approval.expires_at <= self.as_of
                or item.approval.requirements_fingerprint != self.requirements.approval.fingerprint
                for item in counted
            )
            or self.fingerprint
            != approval_fingerprint("review_result", self, exclude={"fingerprint", "as_of"})
        ):
            raise ValueError("approval review differs from its current, distinct signers")
        seen: set[UUID] = set()
        for item in self.entries:
            if (item.approval.approved_by in seen) != (item.state == "superseded"):
                raise ValueError("only the latest recorded statement may count per signer")
            seen.add(item.approval.approved_by)
        if (
            tuple(
                sorted(
                    self.entries,
                    key=lambda item: (item.approval.approved_at, str(item.approval.id)),
                    reverse=True,
                )
            )
            != self.entries
        ):
            raise ValueError("approval review must retain original statement ordering")
        return self
