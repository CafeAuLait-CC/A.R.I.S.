from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand, bounded_json
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, GLOBAL_SCOPE
from aris.modules.nova.policy_history_record_schemas import RecordHistoricalPolicyReview
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService


class RecordHistoricalPolicyReviewCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="policy.record_historical_review",
        required_permission=DRAFT_PERMISSION,
        requires_confirmation=True,
    )

    def __init__(self, service: PolicyHistoricalReviewService) -> None:
        service.require_writer()
        self.service = service

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = RecordHistoricalPolicyReview.model_validate(parameters)
        result = request.model_dump(mode="json")
        bounded_json(result, 8192)
        return result

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
            session, execution, principal, RecordHistoricalPolicyReview.model_validate(parameters)
        ).model_dump(mode="json")
