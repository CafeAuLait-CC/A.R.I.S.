from uuid import UUID

from pydantic import Field, JsonValue

from aris.core.access.schemas import AccessContract, AccessScope, PermissionName


class CommandDefinition(AccessContract):
    name: PermissionName
    required_permission: PermissionName
    requires_confirmation: bool = True


class CommandContext(AccessContract):
    # Supplied by a trusted authenticated adapter, never copied from parameters.
    actor_id: UUID
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    idempotency_key: str = Field(min_length=1, max_length=128, repr=False)


class CommandRequest(AccessContract):
    command: PermissionName
    scope: AccessScope
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class CommandResult(AccessContract):
    command_id: UUID
    duplicate: bool
    result: dict[str, JsonValue]
