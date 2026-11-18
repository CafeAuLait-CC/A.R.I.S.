import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.schemas import AccessContract, Identifier

MAX_MAPPING_HISTORY_CHANGES = 128
MAX_HISTORICAL_MAPPING_QUERIES = 128


class HistoricalMappingValue(AccessContract):
    user_id: UUID
    confidence: Literal["verified", "unverified"]
    source: str = Field(min_length=1, max_length=64)


class MappingHistoryReference(AccessContract):
    id: UUID
    revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    effective_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("effective_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class MappingHistoryEntry(MappingHistoryReference):
    node_id: Identifier
    os_username: Identifier = Field(repr=False)
    before: HistoricalMappingValue | None
    after: HistoricalMappingValue | None
    event_id: UUID
    actor_id: UUID
    operation_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @model_validator(mode="after")
    def changed_value(self) -> "MappingHistoryEntry":
        if self.before == self.after:
            raise ValueError("mapping history requires a net state change")
        return self


def mapping_history_fingerprint(entry: MappingHistoryEntry) -> str:
    data = entry.model_dump(mode="json", exclude={"fingerprint"})
    body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"aris.identity.mapping.history\x00" + body).hexdigest()


class HistoricalNodeUserQuery(AccessContract):
    node_id: Identifier
    os_username: Identifier = Field(repr=False)
    start_at: AwareDatetime
    end_at: AwareDatetime
    as_of: AwareDatetime
    through_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)

    @field_validator("start_at", "end_at", "as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def historical_window(self) -> "HistoricalNodeUserQuery":
        if not self.start_at < self.end_at <= self.as_of:
            raise ValueError("mapping lookup requires a past half-open interval and cutoff")
        return self


class HistoricalNodeUserResolution(AccessContract):
    assessment_scope: Literal["historical_node_identity"] = "historical_node_identity"
    query: HistoricalNodeUserQuery = Field(repr=False)
    status: Literal["resolved", "unknown", "needs_review"]
    reason: Literal[
        "VERIFIED_USER_COVERS_INTERVAL",
        "DIRECTORY_UNINITIALIZED",
        "AS_OF_NOT_REACHED",
        "HISTORY_DOES_NOT_COVER_INTERVAL",
        "EXPLICITLY_UNMAPPED",
        "UNVERIFIED_MAPPING",
        "MAPPING_CHANGED_DURING_INTERVAL",
    ]
    user_id: UUID | None = None
    directory_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)
    evidence: tuple[MappingHistoryReference, ...] = Field(
        default=(), max_length=MAX_MAPPING_HISTORY_CHANGES + 1
    )

    @model_validator(mode="after")
    def honest_identity(self) -> "HistoricalNodeUserResolution":
        if (self.status == "resolved") != (self.user_id is not None):
            raise ValueError("only a resolved historical mapping can identify a platform user")
        if self.status == "resolved" and (
            not self.evidence
            or self.directory_revision is None
            or self.reason != "VERIFIED_USER_COVERS_INTERVAL"
        ):
            raise ValueError("resolved mapping requires verified history evidence")
        if self.status != "resolved" and self.reason == "VERIFIED_USER_COVERS_INTERVAL":
            raise ValueError("unresolved mapping cannot claim verified coverage")
        if (
            self.reason
            in {"DIRECTORY_UNINITIALIZED", "AS_OF_NOT_REACHED", "HISTORY_DOES_NOT_COVER_INTERVAL"}
            and self.status != "unknown"
        ):
            raise ValueError("missing historical knowledge must remain unknown")
        if (
            self.reason in {"UNVERIFIED_MAPPING", "MAPPING_CHANGED_DURING_INTERVAL"}
            and self.status != "needs_review"
        ):
            raise ValueError("ambiguous identity evidence requires review")
        if self.evidence:
            if (
                self.directory_revision is None
                or self.evidence[0].effective_at > self.query.start_at
                or any(
                    item.revision > self.directory_revision or item.effective_at > self.query.as_of
                    for item in self.evidence
                )
                or len({item.id for item in self.evidence}) != len(self.evidence)
                or any(
                    a.revision >= b.revision or a.effective_at > b.effective_at
                    for a, b in zip(self.evidence, self.evidence[1:], strict=False)
                )
            ):
                raise ValueError("historical identity references differ from the knowledge cutoff")
        return self


class HistoricalNodeUserBatch(AccessContract):
    queries: tuple[HistoricalNodeUserQuery, ...] = Field(
        min_length=1, max_length=MAX_HISTORICAL_MAPPING_QUERIES
    )

    @model_validator(mode="after")
    def common_cutoff(self) -> "HistoricalNodeUserBatch":
        if len({(item.as_of, item.through_revision) for item in self.queries}) != 1:
            raise ValueError("historical identity batch requires one knowledge cutoff")
        return self


class HistoricalNodeUserBatchResult(AccessContract):
    as_of: AwareDatetime
    directory_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)
    answers: tuple[HistoricalNodeUserResolution, ...] = Field(
        min_length=1, max_length=MAX_HISTORICAL_MAPPING_QUERIES
    )

    @model_validator(mode="after")
    def same_snapshot(self) -> "HistoricalNodeUserBatchResult":
        if any(
            item.query.as_of != self.as_of
            or item.directory_revision != self.directory_revision
            or (
                self.directory_revision is not None
                and item.query.through_revision != self.directory_revision
            )
            for item in self.answers
        ):
            raise ValueError("historical identity answers differ from the batch snapshot")
        return self
