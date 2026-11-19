"""Registered internal database jobs; task identity is derived from its durable key."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from aris.core.access.schemas import AccessContract, AccessScope

MAX_TASK_BYTES = 16384


def task_identity(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, "aris.scheduler.task/" + key)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ScheduledTask(AccessContract):
    key: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    owner_id: UUID
    scope: AccessScope
    scheduled_for: AwareDatetime
    payload: dict[str, JsonValue] = Field(max_length=32, repr=False)

    @field_validator("scheduled_for")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded(self) -> "ScheduledTask":
        if len(canonical(self.model_dump(mode="json")).encode()) > MAX_TASK_BYTES:
            raise ValueError("scheduled task payload exceeds its budget")
        return self

    @property
    def id(self) -> UUID:
        return task_identity(self.key)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            b"aris.scheduler.task\x00" + canonical(self.model_dump(mode="json")).encode()
        ).hexdigest()


class SchedulerContext(AccessContract):
    request_id: str = Field(min_length=1, max_length=128)
    actor: Literal["system:scheduler"] = "system:scheduler"


class SchedulerLimits(AccessContract):
    transaction_timeout_ms: int = Field(default=10000, ge=1000, le=30000, strict=True)
    lease_seconds: int = Field(default=60, ge=2, le=600, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=10, strict=True)
    retry_base_seconds: int = Field(default=5, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def lease_outlives_transaction(self) -> "SchedulerLimits":
        if self.lease_seconds * 1000 <= self.transaction_timeout_ms:
            raise ValueError("scheduler lease must exceed transaction timeout")
        return self


class StoredScheduledTask(AccessContract):
    request: ScheduledTask
    state: Literal["pending", "running", "retry", "completed", "blocked", "dead"]
    attempts: int = Field(ge=0, le=10)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    available_at: AwareDatetime
    run_id: UUID | None = None
    lease_token: UUID | None = None
    lease_expires_at: AwareDatetime | None = None
    result: dict[str, JsonValue] | None = Field(default=None, repr=False)
    error_code: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> "StoredScheduledTask":
        running = self.state == "running"
        if (
            (self.attempts == 0) != (self.run_id is None)
            or running != (self.lease_token is not None)
            or running != (self.lease_expires_at is not None)
            or (self.state == "completed") != (self.result is not None)
            or (self.state in ("retry", "blocked", "dead")) != (self.error_code is not None)
            or self.updated_at < self.created_at
            or self.available_at < self.created_at
        ):
            raise ValueError("scheduled task state is inconsistent")
        return self


class TaskClaim(AccessContract):
    job_id: UUID
    run_id: UUID
    lease_token: UUID
    attempt: int = Field(ge=1, le=10)
    expires_at: AwareDatetime
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class StoredTaskRun(AccessContract):
    id: UUID
    job_id: UUID
    attempt: int = Field(ge=1, le=10)
    lease_token: UUID
    started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    finished_at: AwareDatetime | None
    state: Literal["running", "completed", "retry", "blocked", "dead", "expired"]
    result: dict[str, JsonValue] | None = Field(default=None, repr=False)
    error_code: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> "StoredTaskRun":
        if (
            (self.state == "running") != (self.finished_at is None)
            or (self.state == "completed") != (self.result is not None)
            or (self.state in ("retry", "blocked", "dead", "expired"))
            != (self.error_code is not None)
            or self.lease_expires_at <= self.started_at
            or (self.finished_at is not None and self.finished_at < self.started_at)
        ):
            raise ValueError("scheduled attempt state is inconsistent")
        return self


class TaskEnqueueResult(AccessContract):
    job: StoredScheduledTask
    duplicate: bool


class TaskTick(AccessContract):
    state: Literal["idle", "completed", "retry", "blocked", "dead", "lost_claim"]
    job_id: UUID | None = None
    error_code: str | None = None
