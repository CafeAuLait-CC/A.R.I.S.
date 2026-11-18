"""Explicit unmounted accounting commands; production permission grants are separate."""

from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.modules.lyra.ledger_schemas import ReverseUsageMeasurement, UsageReversalRequest
from aris.modules.lyra.ledger_service import ADJUST_USAGE_PERMISSION, UsageLedgerService


class ReverseUsageMeasurementCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="usage.reverse_measurement",
        required_permission=ADJUST_USAGE_PERMISSION,
    )

    def __init__(self, ledger: UsageLedgerService) -> None:
        self.ledger = ledger

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return ReverseUsageMeasurement.model_validate(parameters).model_dump(mode="json")

    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        request = ReverseUsageMeasurement.model_validate(parameters)
        return self.ledger.adjustment_scope(session, request.target_id)

    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        request = UsageReversalRequest.model_validate(
            {**parameters, "id": str(execution.command_id)}
        )
        return self.ledger.reverse_in_transaction(
            session, execution, principal, request
        ).model_dump(mode="json")
