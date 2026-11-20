"""Only confirmed human feedback; approval and publication are separate actions."""

from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand, bounded_json
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, GLOBAL_SCOPE
from aris.modules.nova.policy_feedback_schemas import RecordPolicyFeedback
from aris.modules.nova.policy_feedback_service import PolicyFeedbackService


class RecordPolicyFeedbackCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="policy.record_feedback",
        required_permission=DRAFT_PERMISSION,
        requires_confirmation=True,
    )

    def __init__(self, service: PolicyFeedbackService) -> None:
        service.require_writer()
        self.service = service

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = RecordPolicyFeedback.model_validate(parameters)
        value = request.model_dump(mode="json")
        bounded_json(value, 12288)
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
        return self.service.record_in_transaction(
            session, execution, principal, RecordPolicyFeedback.model_validate(parameters)
        ).model_dump(mode="json")
