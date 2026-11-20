from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand, bounded_json
from aris.modules.nova.policy_approval_schemas import ApprovePolicySource
from aris.modules.nova.policy_approval_service import APPROVE_PERMISSION, PolicyApprovalService
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE


class ApprovePolicySourceCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="policy.approve_source",
        required_permission=APPROVE_PERMISSION,
        requires_confirmation=True,
    )

    def __init__(self, service: PolicyApprovalService) -> None:
        service.require_writer()
        self.service = service

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = ApprovePolicySource.model_validate(parameters)
        value = request.model_dump(mode="json")
        bounded_json(value, 8192)
        return value

    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        return GLOBAL_SCOPE

    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return self.service.approve_in_transaction(
            session, execution, principal, ApprovePolicySource.model_validate(parameters)
        ).model_dump(mode="json")
