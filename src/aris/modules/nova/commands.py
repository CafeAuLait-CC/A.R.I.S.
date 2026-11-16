from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.modules.nova.reservation_schemas import CancelReservation, CreateReservation
from aris.modules.nova.reservation_service import ReservationService


class CreateReservationCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="reservation.create", required_permission="reservation.create"
    )

    def __init__(self, reservations: ReservationService) -> None:
        self.reservations = reservations

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return CreateReservation.model_validate(parameters).model_dump(mode="json")

    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        request = CreateReservation.model_validate(parameters)
        return self.reservations.target_scope(session, request.gpu_uuid)

    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return self.reservations.create_in_transaction(
            session, execution, principal, CreateReservation.model_validate(parameters)
        ).model_dump(mode="json")


class CancelReservationCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="reservation.cancel", required_permission="reservation.cancel_own"
    )

    def __init__(self, reservations: ReservationService) -> None:
        self.reservations = reservations

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return CancelReservation.model_validate(parameters).model_dump(mode="json")

    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        return AccessScope(type="global", id="*")

    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return self.reservations.cancel_in_transaction(
            session, execution, principal, CancelReservation.model_validate(parameters)
        ).model_dump(mode="json")
