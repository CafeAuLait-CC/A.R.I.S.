from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.core.relay.schemas import ReplayDelivery
from aris.core.relay.service import EventRelay


class ReplayEventCommand(DatabaseCommand):
    definition = CommandDefinition(name="events.replay", required_permission="events.replay")

    def __init__(self, relay: EventRelay) -> None:
        self.relay = relay

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = ReplayDelivery.model_validate(parameters)
        return request.model_copy(update={"reason": request.reason.strip()}).model_dump(mode="json")

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
        return self.relay.replay(
            session, execution, principal, ReplayDelivery.model_validate(parameters)
        )
