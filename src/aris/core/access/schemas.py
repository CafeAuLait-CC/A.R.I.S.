from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, field_validator, model_validator

from aris.core.contracts import Contract

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
PermissionName = Annotated[
    str, Field(max_length=128, pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
]


class AccessContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ExternalSubject(AccessContract):
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    namespace: Identifier
    external_id: Identifier


class ExternalUserLookup(AccessContract):
    user_id: UUID
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    namespace: Identifier


class ExternalUserTarget(AccessContract):
    user_id: UUID
    subject: ExternalSubject


class AccessScope(AccessContract):
    type: Literal["global", "node", "project"]
    id: Identifier

    @model_validator(mode="after")
    def exact_scope(self) -> "AccessScope":
        if (self.type == "global") != (self.id == "*"):
            raise ValueError("global scope uses *, other scopes require an exact ID")
        return self


class AccessRequest(AccessContract):
    user_id: UUID
    permission: PermissionName
    scope: AccessScope


class AccessRequirement(AccessContract):
    permission: PermissionName
    scope: AccessScope


class AuthorizedUsersQuery(AccessContract):
    """Current active users in one namespace holding every exact requirement."""

    user_ids: tuple[UUID, ...] = Field(max_length=128)
    provider: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    namespace: Identifier
    requirements: tuple[AccessRequirement, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_inputs(self) -> "AuthorizedUsersQuery":
        if len(set(self.user_ids)) != len(self.user_ids) or len(
            {(item.permission, item.scope.type, item.scope.id) for item in self.requirements}
        ) != len(self.requirements):
            raise ValueError("batch access requires unique users and exact requirements")
        return self


class NodeUserLookup(AccessContract):
    node_id: Identifier
    os_username: Identifier


class UserPrincipal(AccessContract):
    user_id: UUID
    display_name: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("user timezone must be a known IANA timezone") from None
        return value


class UserLabelQuery(AccessContract):
    """Presentation only; callers must authorize their domain read first."""

    user_ids: tuple[UUID, ...] = Field(max_length=128)


class UserLabel(AccessContract):
    user_id: UUID
    display_name: str = Field(min_length=1, max_length=128)
