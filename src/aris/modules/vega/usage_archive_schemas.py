"""Contiguous archived messages are not complete time-window or usage coverage."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.vega.usage_schemas import UsageFactContract, UsageObservationFact

MAX_ARCHIVE_SEQ = 9223372036854775807
MAX_ARCHIVE_MESSAGES = 128
# Source JSON is checked before loading it. A Unicode character occupies at most
# four UTF-8 bytes, so the worst-case fetched source bodies stay below 8 MiB.
MAX_ARCHIVE_SOURCE_CHARACTERS = 2 * 1024 * 1024


class UsageArchiveHeadQuery(UsageFactContract):
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")


class UsageArchiveHead(UsageFactContract):
    assessment_scope: Literal["archive_source_ceiling"] = "archive_source_ceiling"
    sequence_coverage: Literal["not_assessed"] = "not_assessed"
    payload_integrity: Literal["not_assessed"] = "not_assessed"
    quota_usage_available: Literal[False] = False
    query: UsageArchiveHeadQuery
    highest_committed_seq: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fixed_ceiling(self) -> "UsageArchiveHead":
        if self.fingerprint != archive_head_fingerprint(self):
            raise ValueError("archive source ceiling fingerprint differs")
        return self


class UsageArchiveFreeze(UsageFactContract):
    source: UsageArchiveHeadQuery
    start_at: AwareDatetime
    end_at: AwareDatetime
    read_at: AwareDatetime
    through_seq: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)

    @field_validator("start_at", "end_at", "read_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def closed_interval(self) -> "UsageArchiveFreeze":
        if not self.start_at < self.end_at <= self.read_at:
            raise ValueError("Archive freeze requires a closed observation interval")
        return self


def archive_head_fingerprint(head: UsageArchiveHead) -> str:
    raw = json.dumps(
        head.model_dump(mode="json", exclude={"fingerprint", "read_at"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.vega.archive.ceiling\x00" + raw.encode()).hexdigest()


class UsageArchiveQuery(UsageFactContract):
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    after_seq: int = Field(ge=0, lt=MAX_ARCHIVE_SEQ, strict=True)
    through_seq: int = Field(ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    limit: int = Field(default=128, ge=1, le=MAX_ARCHIVE_MESSAGES, strict=True)
    source_character_budget: int = Field(
        default=MAX_ARCHIVE_SOURCE_CHARACTERS, ge=1, le=MAX_ARCHIVE_SOURCE_CHARACTERS, strict=True
    )

    @model_validator(mode="after")
    def increasing_range(self) -> "UsageArchiveQuery":
        if self.through_seq <= self.after_seq:
            raise ValueError("archived usage requires an increasing source range")
        return self

    @property
    def page_through_seq(self) -> int:
        return min(self.through_seq, self.after_seq + self.limit)


class ArchivedUsageReference(UsageFactContract):
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    message_id: UUID
    seq: int = Field(ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    kind: Literal["heartbeat", "telemetry"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime
    received_at: AwareDatetime

    @field_validator("observed_at", "received_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ArchivedUsageItem(UsageFactContract):
    reference: ArchivedUsageReference
    observation: UsageObservationFact | None = None

    @model_validator(mode="after")
    def original_source(self) -> "ArchivedUsageItem":
        ref, fact = self.reference, self.observation
        if (ref.kind == "telemetry") != (fact is not None):
            raise ValueError("only telemetry can contain an archived compute observation")
        if fact is not None and (
            fact.agent_id,
            fact.node_id,
            fact.message_id,
            fact.seq,
            fact.source_fingerprint,
            fact.observed_at,
            fact.received_at,
        ) != (
            ref.agent_id,
            ref.node_id,
            ref.message_id,
            ref.seq,
            ref.fingerprint,
            ref.observed_at,
            ref.received_at,
        ):
            raise ValueError("archived observation differs from its source reference")
        return self


class UsageArchivePage(UsageFactContract):
    assessment_scope: Literal["contiguous_archived_messages"] = "contiguous_archived_messages"
    quota_usage_available: Literal[False] = False
    query: UsageArchiveQuery
    items: tuple[ArchivedUsageItem, ...] = Field(max_length=MAX_ARCHIVE_MESSAGES)
    source_characters: int = Field(ge=0, le=MAX_ARCHIVE_SOURCE_CHARACTERS, strict=True)
    next_after_seq: int = Field(ge=0, le=MAX_ARCHIVE_SEQ, strict=True)
    state: Literal["range_end", "page_end", "gap"]
    missing_seq: int | None = Field(default=None, ge=1, le=MAX_ARCHIVE_SEQ, strict=True)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("read_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def contiguous_page(self) -> "UsageArchivePage":
        query = self.query
        expected = query.after_seq + len(self.items)
        state = (
            "range_end"
            if expected == query.through_seq
            else ("page_end" if expected == query.page_through_seq else "gap")
        )
        if (
            len(self.items) > query.limit
            or (bool(self.items) != (self.source_characters > 0))
            or self.source_characters < len(self.items)
            or self.source_characters > query.source_character_budget
            or self.next_after_seq != expected
            or expected > query.through_seq
            or self.state != state
            or self.missing_seq != (expected + 1 if state == "gap" else None)
            or any(
                (item.reference.agent_id, item.reference.node_id, item.reference.seq)
                != (query.agent_id, query.node_id, query.after_seq + index)
                or item.reference.received_at > self.read_at
                for index, item in enumerate(self.items, start=1)
            )
            or len({item.reference.message_id for item in self.items}) != len(self.items)
            or self.fingerprint != archive_page_fingerprint(self)
        ):
            raise ValueError("archive page differs from its exact contiguous source prefix")
        return self


def archive_page_fingerprint(page: UsageArchivePage) -> str:
    raw = json.dumps(
        page.model_dump(mode="json", exclude={"fingerprint", "read_at"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.vega.archive.page\x00" + raw.encode()).hexdigest()
