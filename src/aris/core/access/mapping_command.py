from typing import Annotated

from pydantic import Field, JsonValue, field_validator
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    AssignNodeUser,
    RegisterIdentity,
    RemoveNodeUser,
)
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.schemas import AccessContract, AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.core.contracts import DomainError

MappingChange = Annotated[
    RegisterIdentity | AssignNodeUser | RemoveNodeUser, Field(discriminator="type")
]


class IdentityMappingRequest(AccessContract):
    expected_revision: int = Field(ge=1, strict=True)
    changes: tuple[MappingChange, ...] = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity mapping reason must not be blank")
        return value.strip()


class IdentityMappingCommand(DatabaseCommand):
    definition = CommandDefinition(name="identity.map_users", required_permission="identity.manage")

    def __init__(self, administration: AccessAdministrationService) -> None:
        self.administration = administration

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return IdentityMappingRequest.model_validate(parameters).model_dump(mode="json")

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
        if (
            execution.scope != AccessScope(type="global", id="*")
            or principal.user_id != execution.actor_id
        ):
            raise DomainError("PERMISSION_DENIED", "Directory actor or scope differs", 403)
        request = IdentityMappingRequest.model_validate(parameters)
        return self.administration.apply_in_transaction(
            session,
            AdministrationContext(
                actor_id=execution.actor_id,
                request_id=execution.request_id,
                operation_id=execution.command_id,
                reason=request.reason,
            ),
            AdministrationBatch(
                changes=request.changes, expected_revision=request.expected_revision
            ),
        ).model_dump(mode="json")
