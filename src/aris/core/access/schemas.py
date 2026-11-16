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
