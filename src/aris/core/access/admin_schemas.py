from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, SecretStr, field_validator, model_validator

from aris.core.access.schemas import (
    AccessContract,
    AccessScope,
    ExternalSubject,
    Identifier,
    PermissionName,
)

RoleName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
RequestID = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]


class IdentityNamespace(AccessContract):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    namespace: Identifier


class RegistrationRoleGrant(AccessContract):
    role_name: RoleName
    scope: AccessScope


class DirectoryProfile(AccessContract):
    display_name: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=128)

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display name must not be blank")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("timezone must be a known IANA timezone") from None
        return value


class BootstrapRequest(AccessContract):
    subject: ExternalSubject
    profile: DirectoryProfile
    operator_proof: SecretStr = Field(min_length=32, max_length=256, repr=False)
    request_id: RequestID
    operation_id: UUID = Field(default_factory=uuid4)


class BootstrapResult(AccessContract):
    state: Literal["created", "already_initialized"]
    user_id: UUID
    revision: int = Field(ge=1)


class BootstrapStatus(AccessContract):
    state: Literal["empty", "initialized", "adoption_required"]
    revision: int = Field(ge=0)
    namespace: IdentityNamespace | None = None


class RegistrationMapping(AccessContract):
    node_id: Identifier
    os_username: Identifier
    expected_user_id: UUID | None = None


class RegisterIdentity(AccessContract):
    type: Literal["register_identity"] = "register_identity"
    subject: ExternalSubject
    profile: DirectoryProfile
    mappings: tuple[RegistrationMapping, ...] = Field(default=(), max_length=8)


class AssignNodeUser(RegistrationMapping):
    type: Literal["assign_node_user"] = "assign_node_user"
    user_id: UUID


class RemoveNodeUser(AccessContract):
    type: Literal["remove_node_user"] = "remove_node_user"
    node_id: Identifier
    os_username: Identifier
    expected_user_id: UUID


class DefineRole(AccessContract):
    type: Literal["define_role"] = "define_role"
    name: RoleName
    permissions: tuple[PermissionName, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_permissions(self) -> "DefineRole":
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("role permissions must be unique")
        return self


class BindRole(AccessContract):
    type: Literal["bind_role"] = "bind_role"
    user_id: UUID
    role_name: RoleName
    scope: AccessScope
    present: bool = Field(strict=True)


class UpdateUser(AccessContract):
    type: Literal["update_user"] = "update_user"
    user_id: UUID
    profile: DirectoryProfile
    status: Literal["active", "suspended"]


DirectoryChange = Annotated[
    RegisterIdentity | AssignNodeUser | RemoveNodeUser | DefineRole | BindRole | UpdateUser,
    Field(discriminator="type"),
]


class AdministrationBatch(AccessContract):
    changes: tuple[DirectoryChange, ...] = Field(min_length=1, max_length=32)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)


class AdministrationContext(AccessContract):
    # A future authenticated adapter supplies this, never raw request parameters.
    actor_id: UUID
    request_id: RequestID
    operation_id: UUID = Field(default_factory=uuid4)
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("administrative reason must not be blank")
        return value.strip()


class ChangeOutcome(AccessContract):
    type: str
    entity_id: str
    changed: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class AdministrationResult(AccessContract):
    revision: int = Field(ge=1)
    changed: bool
    outcomes: tuple[ChangeOutcome, ...]
