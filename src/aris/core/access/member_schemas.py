from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from aris.core.access.admin_schemas import (
    AssignNodeUser,
    DirectoryProfile,
    RegisterIdentity,
    RemoveNodeUser,
    RoleName,
)
from aris.core.access.schemas import AccessContract, AccessScope


class ExistingRoleBinding(AccessContract):
    role_name: RoleName
    scope: AccessScope


class EditMember(AccessContract):
    type: Literal["edit_member"] = "edit_member"
    user_id: UUID
    profile: DirectoryProfile | None = None
    active: bool | None = Field(default=None, strict=True)
    role: Literal["admin", "member"] | None = None
    previous_bindings: tuple[ExistingRoleBinding, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def explicit_changes(self) -> "EditMember":
        if self.profile is None and self.active is None and self.role is None:
            raise ValueError("a member edit requires a change")
        if self.role is None and self.previous_bindings:
            raise ValueError("previous access bindings require an explicit replacement role")
        return self


class IssueLoginCredential(AccessContract):
    type: Literal["issue_login_credential"] = "issue_login_credential"
    user_id: UUID
    token_digest: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    expected_generation: int | None = Field(default=None, ge=1, strict=True)


MemberChange = Annotated[
    RegisterIdentity | EditMember | AssignNodeUser | RemoveNodeUser | IssueLoginCredential,
    Field(discriminator="type"),
]


class ManageMembersRequest(AccessContract):
    expected_revision: int = Field(ge=1, strict=True)
    changes: tuple[MemberChange, ...] = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=256, repr=False)
    role_policy_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ProfileEditRequest(AccessContract):
    expected_revision: int = Field(ge=1, strict=True)
    profile: DirectoryProfile


class MemberAccessSummary(AccessContract):
    user_id: UUID
    role: Literal["admin", "member"]
    bindings: tuple[ExistingRoleBinding, ...]
    revoked_when_member: tuple[ExistingRoleBinding, ...] = ()
    credential_generation: int | None
    credential_available: bool


class PortalIdentity(AccessContract):
    user_id: UUID
    display_name: str
    timezone: str
    role: Literal["admin", "member"]
    directory_revision: int
    role_policy: dict[str, JsonValue]
