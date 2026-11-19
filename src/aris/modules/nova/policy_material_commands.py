"""Confirmed example recording, never registration of approve/publish capabilities."""

from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand, bounded_json
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, GLOBAL_SCOPE
from aris.modules.nova.policy_material_schemas import RecordPolicyReviewMaterial
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService


class RecordPolicyReviewMaterialCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="policy.record_review_material",
        required_permission=DRAFT_PERMISSION,
        requires_confirmation=True,
    )

    def __init__(self, service: PolicyReviewMaterialService) -> None:
        service.require_writer()
        self.service = service

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = RecordPolicyReviewMaterial.model_validate(parameters)
        # Keep normalization independent of later runtime configuration so a
        # committed command can still be recovered with its original key.
        value = request.model_dump(mode="json")
        bounded_json(value, 16384)
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
            session, execution, principal, RecordPolicyReviewMaterial.model_validate(parameters)
        ).model_dump(mode="json")
