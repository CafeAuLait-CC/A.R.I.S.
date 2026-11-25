"""Trusted maintenance contracts; no public route or automatic expiry policy."""

import hashlib
import json
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import Contract
from aris.core.relay.schemas import EventEnvelope


def digest(value: Contract) -> str:
    raw = json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(b"aris.core.log-expiry\x00" + raw.encode()).hexdigest()


class LogScope(Contract):
    event_types: tuple[str, ...] = Field(min_length=1, max_length=8)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    through_seq: int = Field(ge=1)
    before: AwareDatetime


class AuditSnapshot(Contract):
    id: UUID
    actor: str
    action: str
    target: str
    request_id: str
    recorded_at: AwareDatetime
    changes: dict[str, str | int | bool]


class LogPair(Contract):
    event: EventEnvelope
    audit: AuditSnapshot


class LogTarget(Contract):
    event_id: UUID
    event_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    audit_id: UUID
    audit_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def from_pair(cls, pair: LogPair) -> "LogTarget":
        return cls(
            event_id=pair.event.event_id,
            event_fingerprint=digest(pair.event),
            audit_id=pair.audit.id,
            audit_fingerprint=digest(pair.audit),
        )


class LogExpiryRequest(Contract):
    id: UUID
    scope: LogScope
    source_evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    targets: tuple[LogTarget, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def distinct(self) -> "LogExpiryRequest":
        if (
            len({row.event_id for row in self.targets}) != len(self.targets)
            or len({row.audit_id for row in self.targets}) != len(self.targets)
            or self.id in {row.audit_id for row in self.targets}
        ):
            raise ValueError("Log expiry requires distinct records and an independent operation ID")
        return self

    @property
    def fingerprint(self) -> str:
        return digest(self)


class LogExpiryResult(Contract):
    id: UUID
    events: int
    audits: int
    duplicate: bool
