from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.time import utc
from aris.modules.nova.availability_schemas import (
    AvailabilityContract,
    AvailabilityQuery,
    AvailabilityResult,
    MaintenanceCoverage,
)
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


class CreationEvidence(AvailabilityContract):
    reservation: ReservationView
    assessment_scope: Literal["basic_window"]
    command_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    assessment: AvailabilityResult
    maintenance_snapshot: MaintenanceCoverage
    maintenance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent_creation(self) -> "CreationEvidence":
        view, decision, known = self.reservation, self.assessment, self.maintenance_snapshot
        if (
            view.status != "confirmed"
            or view.version != 1
            or view.policy_version is not None
            or decision.code != "AVAILABLE"
            or decision.resource is None
            or decision.resource.gpu_uuid != view.gpu_uuid
            or decision.query.gpu_uuid != view.gpu_uuid
            or decision.query.start_at != view.start_at
            or decision.query.end_at != view.end_at
            or decision.generated_at != view.updated_at
            or known.gpu_uuid != view.gpu_uuid
            or known.coverage.start_at > view.start_at
            or known.coverage.end_at < view.end_at
            or any(
                window.start_at < view.end_at and window.end_at > view.start_at
                for window in known.windows
            )
            or known.fingerprint != self.maintenance_fingerprint
        ):
            raise ValueError("creation evidence does not match its reservation")
        return self


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
