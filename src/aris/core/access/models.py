from typing import Literal

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aris.core.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("status IN ('active','suspended')", name="ck_user_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[Literal["active", "suspended"]] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(128))


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "namespace", "external_id", name="uq_external_identity"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    namespace: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("name", name="uq_access_role_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("name", name="uq_access_permission_name"),
        CheckConstraint("name <> '*'", name="ck_no_wildcard_permission"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), primary_key=True)
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id"), primary_key=True)


class RoleBinding(Base):
    __tablename__ = "role_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", name="uq_role_binding"),
        CheckConstraint("scope_type IN ('global','node','project')", name="ck_role_scope_type"),
        CheckConstraint(
            "(scope_type='global' AND scope_id='*') OR "
            "(scope_type<>'global' AND scope_id<>'' AND scope_id<>'*')",
            name="ck_role_scope_id",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    scope_type: Mapped[Literal["global", "node", "project"]] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(128))


class NodeUserMapping(Base):
    __tablename__ = "node_user_mappings"
    __table_args__ = (
        CheckConstraint("confidence IN ('verified','unverified')", name="ck_mapping_confidence"),
    )
    # Opaque scope ID: Core never references or queries a concrete domain table.
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    os_username: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    confidence: Mapped[Literal["verified", "unverified"]] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(64))
