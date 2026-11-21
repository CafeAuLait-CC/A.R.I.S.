"""Dispatch attempts describe preparation progress, not a measurement or coverage result."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.stable_window_plan_schemas import StableWindowDispatchReceipt
from aris.modules.lyra.usage_intervals import IntervalContract


class StableDispatchState(IntervalContract):
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: int = Field(ge=1, le=9223372036854775807, strict=True)
    observed_plan_version: int = Field(ge=1, le=9223372036854775807, strict=True)
    failures: int = Field(ge=0, le=20, strict=True)
    blocked: bool = Field(strict=True)
    last_error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z_]+$", max_length=64)
    available_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    event_id: UUID

    @field_validator("available_at", "created_at", "updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent_attempt(self) -> "StableDispatchState":
        if (
            (self.failures == 0) != (self.last_error_code is None)
            or (self.blocked and self.failures == 0)
            or self.available_at < self.updated_at
            or self.updated_at < self.created_at
        ):
            raise ValueError("stable dispatch attempt state is inconsistent")
        return self


class StableAutoDispatchReceipt(IntervalContract):
    assessment_scope: Literal["stable_window_preparation_attempt"] = (
        "stable_window_preparation_attempt"
    )
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    state: Literal["idle", "paused", "enqueued", "retry", "blocked", "superseded"]
    dispatch: StableWindowDispatchReceipt | None = None
    attempt: StableDispatchState | None = None

    @model_validator(mode="after")
    def explicit_preparation_result(self) -> "StableAutoDispatchReceipt":
        if (
            (self.state == "enqueued") != (self.dispatch is not None)
            or (self.state in {"enqueued", "retry", "blocked"}) != (self.attempt is not None)
            or (self.attempt is not None and (self.state == "blocked") != self.attempt.blocked)
            or (
                self.dispatch is not None
                and (
                    not self.dispatch.job_ids
                    or self.attempt is None
                    or self.attempt.key != self.dispatch.plan.key
                    or self.attempt.observed_plan_version != self.dispatch.plan.version
                    or self.attempt.failures != 0
                )
            )
        ):
            raise ValueError("automatic stable dispatch differs from its actual preparation result")
        return self
