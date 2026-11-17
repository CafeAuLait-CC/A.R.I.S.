from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.nova.availability_schemas import AvailabilityContract


class CurrentReservationQuery(AvailabilityContract):
    gpu_uuids: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_unique_targets(self) -> "CurrentReservationQuery":
        if len(set(self.gpu_uuids)) != len(self.gpu_uuids) or any(
            not 1 <= len(value) <= 128 for value in self.gpu_uuids
        ):
            raise ValueError("reservation targets must be bounded and unique")
        return self


class GpuReservationOccupancy(AvailabilityContract):
    gpu_uuid: str
    state: Literal["unoccupied", "occupied", "unknown"]
    ends_at: AwareDatetime | None

    @model_validator(mode="after")
    def coherent_end(self) -> "GpuReservationOccupancy":
        if (self.state == "occupied") != (self.ends_at is not None):
            raise ValueError("only unique occupied reservations have an end")
        return self


class CurrentReservationSnapshot(AvailabilityContract):
    assessment_scope: Literal["current_reservations"] = "current_reservations"
    as_of: AwareDatetime
    gpus: tuple[GpuReservationOccupancy, ...] = Field(min_length=1, max_length=100)
