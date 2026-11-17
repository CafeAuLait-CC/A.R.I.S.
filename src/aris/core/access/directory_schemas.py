from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from aris.core.access.admin_schemas import DirectoryProfile, IdentityNamespace
from aris.core.access.schemas import AccessContract, Identifier


class DirectoryQuery(AccessContract):
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    after: Identifier | None = None
    external_ids: tuple[Identifier, ...] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def unique_nodes(self) -> "DirectoryQuery":
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("directory nodes must be unique")
        if self.external_ids is not None and (
            self.after is not None
            or len(self.external_ids) > self.limit
            or len(set(self.external_ids)) != len(self.external_ids)
        ):
            raise ValueError(
                "directory selection must be unique, bounded and separate from a cursor"
            )
        return self


class DirectoryMapping(AccessContract):
    user_id: UUID
    node_id: Identifier
    os_username: Identifier
    confidence: Literal["verified", "unverified"]
    source: str = Field(min_length=1, max_length=64)


class DirectoryMember(AccessContract):
    user_id: UUID
    external_id: Identifier
    profile: DirectoryProfile
    status: Literal["active", "suspended"]
    mappings: tuple[DirectoryMapping, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def owned_mappings(self) -> "DirectoryMember":
        if any(mapping.user_id != self.user_id for mapping in self.mappings):
            raise ValueError("directory mapping owner differs")
        return self


class DirectoryPage(AccessContract):
    namespace: IdentityNamespace
    revision: int = Field(ge=1)
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    members: tuple[DirectoryMember, ...] = Field(max_length=50)
    next_cursor: Identifier | None = None

    @model_validator(mode="after")
    def coherent_page(self) -> "DirectoryPage":
        identifiers = [member.external_id for member in self.members]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("directory page requires unique external identities")
        if self.next_cursor is not None and (
            not identifiers or self.next_cursor != identifiers[-1]
        ):
            raise ValueError("directory cursor differs from the page")
        if any(
            mapping.node_id not in self.node_ids
            for member in self.members
            for mapping in member.mappings
        ):
            raise ValueError("directory mapping belongs to an unrequested node")
        return self
