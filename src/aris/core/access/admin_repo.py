from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import DirectoryProfile, IdentityNamespace
from aris.core.access.models import (
    AccessDirectoryState,
    ExternalIdentity,
    NodeUserMapping,
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessScope, ExternalSubject

MANAGE_IDENTITIES = "identity.manage"


class AccessAdministrationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_directory(self) -> AccessDirectoryState:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        self.session.execute(
            insert(AccessDirectoryState).values(id="primary").on_conflict_do_nothing()
        )
        return self.session.scalars(
            select(AccessDirectoryState)
            .where(AccessDirectoryState.id == "primary")
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()

    def has_existing_directory(self) -> bool:
        return any(
            self.session.scalar(select(func.count()).select_from(model))
            for model in (User, ExternalIdentity, Role, RoleBinding, NodeUserMapping)
        )

    def identity(self, subject: ExternalSubject) -> ExternalIdentity | None:
        return self.session.scalar(
            select(ExternalIdentity)
            .where(
                ExternalIdentity.provider == subject.provider,
                ExternalIdentity.namespace == subject.namespace,
                ExternalIdentity.external_id == subject.external_id,
            )
            .execution_options(populate_existing=True)
        )

    def user(self, user_id: str) -> User | None:
        return self.session.scalar(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )

    def create_user(self, subject: ExternalSubject, profile: DirectoryProfile) -> User:
        user = User(
            id=str(uuid4()),
            display_name=profile.display_name,
            status="active",
            timezone=profile.timezone,
        )
        self.session.add(user)
        self.session.flush()
        self.session.add(
            ExternalIdentity(
                id=str(uuid4()),
                provider=subject.provider,
                namespace=subject.namespace,
                external_id=subject.external_id,
                user_id=user.id,
            )
        )
        self.session.flush()
        return user

    def define_role(
        self, name: str, permissions: tuple[str, ...]
    ) -> tuple[Role, bool, tuple[str, ...]]:
        role = self.session.scalar(select(Role).where(Role.name == name))
        created = role is None
        if role is None:
            role = Role(id=str(uuid4()), name=name)
            self.session.add(role)
            self.session.flush()
        current = set(
            self.session.scalars(
                select(Permission.name)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            )
        )
        if current == set(permissions):
            return role, created, tuple(sorted(current))
        self.session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        for name in permissions:
            permission = self.session.scalar(select(Permission).where(Permission.name == name))
            if permission is None:
                permission = Permission(id=str(uuid4()), name=name)
                self.session.add(permission)
                self.session.flush()
            self.session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        return role, True, tuple(sorted(current))

    def role(self, name: str) -> Role | None:
        return self.session.scalar(select(Role).where(Role.name == name))

    def bind_role(self, user_id: str, role_id: str, scope: AccessScope, present: bool) -> bool:
        binding = self.session.scalar(
            select(RoleBinding).where(
                RoleBinding.user_id == user_id,
                RoleBinding.role_id == role_id,
                RoleBinding.scope_type == scope.type,
                RoleBinding.scope_id == scope.id,
            )
        )
        if present and binding is None:
            self.session.add(
                RoleBinding(
                    id=str(uuid4()),
                    user_id=user_id,
                    role_id=role_id,
                    scope_type=scope.type,
                    scope_id=scope.id,
                )
            )
            return True
        if not present and binding is not None:
            self.session.delete(binding)
            return True
        return False

    def mapping(self, node_id: str, username: str) -> NodeUserMapping | None:
        return self.session.scalar(
            select(NodeUserMapping)
            .where(
                NodeUserMapping.node_id == node_id,
                NodeUserMapping.os_username == username,
            )
            .execution_options(populate_existing=True)
        )

    def add_mapping(self, node_id: str, username: str, user_id: str) -> None:
        self.session.add(
            NodeUserMapping(
                node_id=node_id,
                os_username=username,
                user_id=user_id,
                source="manual",
                confidence="verified",
            )
        )

    def remove_mapping(self, mapping: NodeUserMapping) -> None:
        self.session.delete(mapping)

    def has_login_identity(self, user_id: str, namespace: IdentityNamespace) -> bool:
        return (
            self.session.scalar(
                select(ExternalIdentity.id)
                .where(
                    ExternalIdentity.user_id == user_id,
                    ExternalIdentity.provider == namespace.provider,
                    ExternalIdentity.namespace == namespace.namespace,
                )
                .limit(1)
            )
            is not None
        )

    def active_administrators(self, namespace: IdentityNamespace) -> int:
        return int(
            self.session.scalar(
                select(func.count(func.distinct(User.id)))
                .join(RoleBinding, RoleBinding.user_id == User.id)
                .join(RolePermission, RolePermission.role_id == RoleBinding.role_id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                .where(
                    User.status == "active",
                    Permission.name == MANAGE_IDENTITIES,
                    RoleBinding.scope_type == "global",
                    RoleBinding.scope_id == "*",
                    ExternalIdentity.provider == namespace.provider,
                    ExternalIdentity.namespace == namespace.namespace,
                )
            )
            or 0
        )
