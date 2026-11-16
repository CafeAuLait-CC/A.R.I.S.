from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, field_validator, model_validator

from aris.core.contracts import Contract

ReservationStatus = Literal[
    "pending", "confirmed", "active", "completed", "cancelled", "rejected", "reclaiming"
]
BLOCKING_STATUSES = ("confirmed", "active", "reclaiming")


class ReservationWindow(Contract):
    start_at: AwareDatetime
    end_at: AwareDatetime

    @field_validator("start_at", "end_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def positive_window(self) -> "ReservationWindow":
        if self.end_at <= self.start_at:
            raise ValueError("reservation end must follow start")
        return self
