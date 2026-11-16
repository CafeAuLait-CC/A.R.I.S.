from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, SecretStr

from aris.core.access.schemas import AccessContract, AccessScope, PermissionName


class CommandDefinition(AccessContract):
    name: PermissionName
    required_permission: PermissionName
    requires_confirmation: bool = True


class ActorContext(AccessContract):
    # Supplied by a trusted authenticated adapter, never copied from parameters.
    actor_id: UUID
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class CommandContext(ActorContext):
    idempotency_key: str = Field(min_length=1, max_length=128, repr=False)


class CommandExecution(ActorContext):
    command_id: UUID
    scope: AccessScope


class CommandRequest(AccessContract):
    command: PermissionName
    scope: AccessScope
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class CommandResult(AccessContract):
    state: Literal["succeeded"] = "succeeded"
    command_id: UUID
    duplicate: bool
    result: dict[str, JsonValue]


class PendingConfirmation(AccessContract):
    state: Literal["awaiting_confirmation"] = "awaiting_confirmation"
    command_id: UUID
    expires_at: AwareDatetime
    proof: SecretStr


class ExpiredCommand(AccessContract):
    state: Literal["expired"] = "expired"
    command_id: UUID


class ConfirmCommand(AccessContract):
    command_id: UUID
    proof: SecretStr = Field(min_length=64, max_length=64)


CommandOutcome = CommandResult | PendingConfirmation | ExpiredCommand
