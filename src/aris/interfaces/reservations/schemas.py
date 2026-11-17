from pydantic import Field, model_validator

from aris.core.access.schemas import AccessContract
from aris.modules.nova.reservation_schemas import MineResult, ReservationView
from aris.modules.vega.target_schemas import GpuInventoryLabel


class LabeledMinePage(AccessContract):
    page: MineResult = Field(repr=False)
    labels: tuple[GpuInventoryLabel, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def only_requested_labels(self) -> "LabeledMinePage":
        targets = {view.gpu_uuid for view in self.page.reservations}
        seen = [label.gpu_uuid for label in self.labels]
        if len(set(seen)) != len(seen) or set(seen) - targets:
            raise ValueError("labels do not match the personal reservation page")
        return self


class OwnedReservationDetail(AccessContract):
    reservation: ReservationView = Field(repr=False)
    resource_label: GpuInventoryLabel | None = None

    @model_validator(mode="after")
    def matching_label(self) -> "OwnedReservationDetail":
        if (
            self.resource_label is not None
            and self.resource_label.gpu_uuid != self.reservation.gpu_uuid
        ):
            raise ValueError("resource label belongs to another reservation target")
        return self
