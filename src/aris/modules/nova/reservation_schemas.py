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
