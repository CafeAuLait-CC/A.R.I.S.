from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from aris.core.access.schemas import AccessContract, Identifier


class EventSelector(AccessContract):
    event_type: str = Field(min_length=1, max_length=128)
    event_version: int = Field(ge=1, strict=True)


class ConsumerDefinition(AccessContract):
    consumer_id: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    revision: str = Field(max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    events: tuple[EventSelector, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_events(self) -> "ConsumerDefinition":
        if len({(item.event_type, item.event_version) for item in self.events}) != len(self.events):
            raise ValueError("duplicate consumer event selector")
        return self


class EventEnvelope(AccessContract):
    event_id: UUID
    event_type: str = Field(min_length=1, max_length=128)
    event_version: int = Field(ge=1)
    aggregate_type: str = Field(min_length=1, max_length=64)
    aggregate_id: Identifier
    aggregate_version: int = Field(ge=1)
    occurred_at: AwareDatetime
    recorded_at: AwareDatetime
    actor: Identifier
    correlation_id: Identifier
    causation_id: UUID
    payload: dict[str, JsonValue]


class RelayLimits(AccessContract):
    lock_timeout_ms: int = Field(gt=0, le=10000, strict=True)
    statement_timeout_ms: int = Field(gt=0, le=20000, strict=True)
    transaction_timeout_ms: int = Field(gt=0, le=30000, strict=True)
    lease_seconds: int = Field(ge=2, le=600, strict=True)
    max_attempts: int = Field(ge=1, le=20, strict=True)
    retry_base_seconds: int = Field(ge=1, le=60, strict=True)
    batch_size: int = Field(ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def ordered_timeouts(self) -> "RelayLimits":
        if not (
            self.lock_timeout_ms
            < self.statement_timeout_ms
            < self.transaction_timeout_ms
            < self.lease_seconds * 1000
        ):
            raise ValueError("lease must exceed the ordered transaction budgets")
        return self


class PublicationResult(AccessContract):
    published: int = Field(ge=0)
    deliveries_queued: int = Field(ge=0)


class DeliveryClaim(AccessContract):
    delivery_id: UUID
    event_id: UUID
    consumer_id: str
    lease_token: UUID
    lease_expires_at: AwareDatetime
    attempt: int = Field(ge=1)
    replay_count: int = Field(ge=0)


class DeliveryResult(AccessContract):
    delivery_id: UUID
    state: Literal["processed", "dead", "retry", "lost_claim"]
    code: str | None = None


class ReplayDelivery(AccessContract):
    delivery_id: UUID
    expected_replay_count: int = Field(ge=0, strict=True)
    reason: str = Field(min_length=1, max_length=500, pattern=r"\S")
