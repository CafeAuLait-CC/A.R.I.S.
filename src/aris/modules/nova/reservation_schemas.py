from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from aris.core.time import utc
from aris.modules.nova.availability_schemas import AvailabilityContract, AvailabilityQuery
from aris.modules.nova.schemas import ReservationStatus


class CreateReservation(AvailabilityQuery):
    purpose: str = Field(min_length=1, max_length=256)

    @field_validator("purpose")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reservation purpose must not be blank")
        return value.strip()


class ReservationView(AvailabilityContract):
    id: UUID
    gpu_uuid: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    status: ReservationStatus
    purpose: str
    policy_version: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: int | None = Field(ge=1)


class CreatedReservation(AvailabilityContract):
    reservation: ReservationView
    assessment_scope: Literal["basic_window"] = "basic_window"
    rules_fingerprint: str
    maintenance_fingerprint: str


class OwnerReservationChange(AvailabilityContract):
    reservation_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reservation change reason must not be blank")
        return value.strip()


class CancelReservation(OwnerReservationChange):
    pass


class ReleaseReservation(OwnerReservationChange):
    pass


class ReleasedReservation(AvailabilityContract):
    reservation: ReservationView
    released_at: AwareDatetime
    effective_end_at: AwareDatetime


class ExtendReservation(OwnerReservationChange):
    new_end_at: AwareDatetime

    @field_validator("new_end_at")
    @classmethod
    def utc_end(cls, value: datetime) -> datetime:
        return utc(value)


class ExtendedReservation(AvailabilityContract):
    reservation: ReservationView
    previous_end_at: AwareDatetime
    assessment_scope: Literal["extension_delta"] = "extension_delta"
    rules_fingerprint: str
    maintenance_fingerprint: str


class MineCursor(AvailabilityContract):
    created_at: AwareDatetime
    reservation_id: UUID

    @field_validator("created_at")
    @classmethod
    def utc_cursor(cls, value: datetime) -> datetime:
        return utc(value)


class MineQuery(AvailabilityContract):
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    cursor: MineCursor | None = None


class MineResult(AvailabilityContract):
    reservations: tuple[ReservationView, ...]
    next_cursor: MineCursor | None
