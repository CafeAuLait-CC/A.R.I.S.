"""Fixed historical review records never authorize current usage or publication."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_material_schemas import ImplementationReference
from aris.modules.nova.policy_schemas import PolicyContract
from aris.modules.nova.quota_history_schemas import (
    HistoricalPolicyNode,
    HistoricalQuotaQuery,
    HistoricalQuotaReview,
)

MAX_HISTORICAL_REVIEW_BYTES = 2_097_152


def canonical_history_record(value: PolicyContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def historical_context_fingerprint(value: HistoricalQuotaReview, implementation_ref: str) -> str:
    return hashlib.sha256(
        b"aris.nova.historical_review_context\x00"
        + implementation_ref.encode()
        + b"\x00"
        + canonical_history_record(value).encode()
    ).hexdigest()


class HistoricalReviewConfiguration(PolicyContract):
    implementation_ref: ImplementationReference
    nodes: tuple[HistoricalPolicyNode, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_nodes(self) -> "HistoricalReviewConfiguration":
        if len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("historical review nodes must be explicit and unique")
        return self


class RecordHistoricalPolicyReview(PolicyContract):
    query: HistoricalQuotaQuery
    expected_implementation_ref: ImplementationReference
    expected_context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("historical review requires a nonblank single-line reason")
        return value


class StoredHistoricalPolicyReview(PolicyContract):
    historical_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source_id: UUID
    request: RecordHistoricalPolicyReview
    review: HistoricalQuotaReview = Field(repr=False)
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
    def coherent(self) -> "StoredHistoricalPolicyReview":
        if (
            self.request.query != self.review.assessment.query
            or self.request.query.usage.owner_id != self.created_by
            or self.created_at < self.review.evidence.created_at
            or historical_context_fingerprint(self.review, self.request.expected_implementation_ref)
            != self.request.expected_context_fingerprint
            or history_record_fingerprint(self) != self.fingerprint
            or len(canonical_history_record(self).encode()) > MAX_HISTORICAL_REVIEW_BYTES
        ):
            raise ValueError("stored historical review differs from its confirmed original")
        return self


def history_record_fingerprint(value: StoredHistoricalPolicyReview) -> str:
    return hashlib.sha256(
        b"aris.nova.historical_review_record\x00"
        + canonical_history_record(value, exclude={"fingerprint"}).encode()
    ).hexdigest()


class HistoricalReviewSummary(PolicyContract):
    historical_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=128)
    snapshot_id: UUID
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    implementation_ref: ImplementationReference
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class HistoricalReviewCursor(PolicyContract):
    created_at: AwareDatetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class HistoricalReviewListQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=128)
    before: HistoricalReviewCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)


class HistoricalReviewPage(PolicyContract):
    query: HistoricalReviewListQuery
    items: tuple[HistoricalReviewSummary, ...] = Field(max_length=16)
    next_cursor: HistoricalReviewCursor | None = None


class HistoricalReviewDetailQuery(PolicyContract):
    source: PolicySourceReference
    expected_source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=128)
    id: UUID
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class HistoricalReviewReceipt(PolicyContract):
    historical_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    id: UUID
    source: PolicySourceReference
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    created: bool = Field(strict=True)
