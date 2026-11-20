from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import AdministrationContext
from aris.core.access.member_schemas import ManageMembersRequest, ProfileEditRequest
from aris.core.access.members import MemberAdministrationService
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.core.contracts import DomainError


class ManageMembersCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="identity.manage_members", required_permission="identity.manage"
    )

    def __init__(self, members: MemberAdministrationService) -> None:
        self.members = members

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request = ManageMembersRequest.model_validate(parameters)
        if request.role_policy_fingerprint not in (None, self.members.policy_fingerprint):
            raise DomainError("CONFLICT", "Member role policy changed; review again", 409)
        return request.model_copy(
            update={"role_policy_fingerprint": self.members.policy_fingerprint}
        ).model_dump(mode="json")

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
        request = ManageMembersRequest.model_validate(parameters)
        return self.members.manage_in_transaction(
            session,
            AdministrationContext(
                actor_id=principal.user_id,
                request_id=execution.request_id,
                operation_id=execution.command_id,
                reason=request.reason,
            ),
            request,
        )


class EditOwnProfileCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="identity.edit_own_profile", required_permission="identity.edit_profile"
    )

    def __init__(self, members: MemberAdministrationService) -> None:
        self.members = members

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return ProfileEditRequest.model_validate(parameters).model_dump(mode="json")

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
        return self.members.edit_profile_in_transaction(
            session,
            AdministrationContext(
                actor_id=principal.user_id,
                request_id=execution.request_id,
                operation_id=execution.command_id,
                reason="Member updated their own profile",
            ),
            ProfileEditRequest.model_validate(parameters),
        )
