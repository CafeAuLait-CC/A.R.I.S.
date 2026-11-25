"""A trusted maintenance batch is not a public deletion request."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import Contract
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_MESSAGES,
    ArchivedUsageReference,
    UsageArchiveHeadQuery,
)


class PayloadRetirementContext(Contract):
    actor: Literal["system:lyra-maintenance"] = "system:lyra-maintenance"
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class PayloadRetirementRequest(Contract):
    id: UUID
    source: UsageArchiveHeadQuery
    before: AwareDatetime
    dependency_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reports: tuple[ArchivedUsageReference, ...] = Field(
        min_length=1, max_length=MAX_ARCHIVE_MESSAGES
    )

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(b"aris.vega.payload-retirement\x00" + raw.encode()).hexdigest()

    @model_validator(mode="after")
    def contiguous_source(self) -> "PayloadRetirementRequest":
        first = self.reports[0].seq
        if len({row.message_id for row in self.reports}) != len(self.reports) or any(
            (row.agent_id, row.node_id, row.seq)
            != (self.source.agent_id, self.source.node_id, first + index)
            or max(row.observed_at, row.received_at) >= self.before
            for index, row in enumerate(self.reports)
        ):
            raise ValueError("Retirement requires consecutive original reports before its cutoff")
        return self


class PayloadRetirementReceipt(Contract):
    id: UUID
    source: UsageArchiveHeadQuery
    first_seq: int
    through_seq: int
    retired_payloads: int
    fingerprint: str
    duplicate: bool
