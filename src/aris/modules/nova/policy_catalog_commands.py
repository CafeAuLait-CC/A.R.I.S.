from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.modules.nova.policy_catalog_schemas import RecordPolicyDraft
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, PolicyCatalogService
from aris.modules.nova.policy_source import compile_policy


class RecordPolicyDraftCommand(DatabaseCommand):
    definition = CommandDefinition(name="policy.record_draft", required_permission=DRAFT_PERMISSION)

    def __init__(self, catalog: PolicyCatalogService) -> None:
        self.catalog = catalog

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = RecordPolicyDraft.model_validate(parameters)
        compiled = compile_policy(request.source)
        return RecordPolicyDraft(source=compiled.source, reason=request.reason).model_dump(
            mode="json"
        )

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
        return self.catalog.record_in_transaction(
            session, execution, principal, RecordPolicyDraft.model_validate(parameters)
        ).model_dump(mode="json")
