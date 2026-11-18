"""Explicit historical references; reservation evidence is not GPU use evidence."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.nova.availability_schemas import AvailabilityContract

MAX_RESERVATION_LINK_HISTORY = 128


class ReservationUsageQuery(AvailabilityContract):
    reservation_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    owner_id: UUID
    gpu_uuid: str = Field(min_length=1, max_length=128)
    start_at: AwareDatetime
    end_at: AwareDatetime
    as_of: AwareDatetime
    expected_history_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("start_at", "end_at", "as_of")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def past_window(self) -> "ReservationUsageQuery":
        if not self.start_at < self.end_at <= self.as_of:
            raise ValueError("reservation association requires a historical half-open interval")
        return self


class ReservationHistoryReference(AvailabilityContract):
    event_id: UUID
    reservation_id: UUID
    version: int = Field(ge=1, strict=True)
    event_type: Literal[
        "reservation.created",
        "reservation.cancelled",
        "reservation.released",
        "reservation.extended",
    ]
    occurred_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReservationUsageLink(AvailabilityContract):
    assessment_scope: Literal["explicit_historical_reservation"] = "explicit_historical_reservation"
    query: ReservationUsageQuery = Field(repr=False)
    state: Literal["matched", "not_matched", "unknown"]
    reason: Literal[
        "OWNER_RESOURCE_AND_WINDOW_MATCH",
        "RESERVATION_NOT_FOUND",
        "AS_OF_NOT_REACHED",
        "NO_RESERVATION_AT_CUTOFF",
        "OWNER_OR_RESOURCE_DIFFERS",
        "CANCELLED_RESERVATION",
        "WINDOW_NOT_COVERED",
    ]
    history_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence: tuple[ReservationHistoryReference, ...] = Field(
        default=(), max_length=MAX_RESERVATION_LINK_HISTORY
    )

    @model_validator(mode="after")
    def honest_link(self) -> "ReservationUsageLink":
        if (self.state == "matched") != (self.reason == "OWNER_RESOURCE_AND_WINDOW_MATCH"):
            raise ValueError("only full reservation coverage can be linked")
        if self.state == "matched" and (not self.evidence or self.history_fingerprint is None):
            raise ValueError("matched reservation requires exact history evidence")
        if self.evidence and (
            self.history_fingerprint is None
            or self.evidence[-1].version != self.query.expected_version
            or any(
                item.reservation_id != self.query.reservation_id
                or item.version != index + 1
                or item.occurred_at > self.query.as_of
                for index, item in enumerate(self.evidence)
            )
        ):
            raise ValueError("reservation references differ from the explicit history cutoff")
        return self
