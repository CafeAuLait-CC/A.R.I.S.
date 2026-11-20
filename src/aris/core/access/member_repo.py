from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.member_schemas import ExistingRoleBinding
from aris.core.access.models import (
    ExternalIdentity,
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessScope


class MemberRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def bindings(self, user_id: str) -> tuple[ExistingRoleBinding, ...]:
        return self.bindings_for((user_id,)).get(user_id, ())

    def bindings_for(self, users: tuple[str, ...]) -> dict[str, tuple[ExistingRoleBinding, ...]]:
        rows = self.session.execute(
            select(RoleBinding.user_id, Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
            .join(Role, Role.id == RoleBinding.role_id)
            .where(RoleBinding.user_id.in_(users))
            .order_by(Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
            .limit(len(users) * 33 + 1)
        )
        result: dict[str, list[ExistingRoleBinding]] = {}
        for user, name, kind, scope in rows:
            result.setdefault(user, []).append(
                ExistingRoleBinding(role_name=name, scope=AccessScope(type=kind, id=scope))
            )
        return {user: tuple(bindings) for user, bindings in result.items()}

    def namespace_users(
        self, users: tuple[str, ...], namespace: IdentityNamespace
    ) -> frozenset[str]:
        return frozenset(
            self.session.scalars(
                select(ExternalIdentity.user_id).where(
                    ExternalIdentity.user_id.in_(users),
                    ExternalIdentity.provider == namespace.provider,
                    ExternalIdentity.namespace == namespace.namespace,
                )
            )
        )

    def administrator_users(self, users: tuple[str, ...]) -> frozenset[str]:
        return frozenset(
            self.session.scalars(
                select(RoleBinding.user_id)
                .join(RolePermission, RolePermission.role_id == RoleBinding.role_id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(
                    RoleBinding.user_id.in_(users),
                    RoleBinding.scope_type == "global",
                    RoleBinding.scope_id == "*",
                    Permission.name == "identity.manage",
                )
            )
        )

    def in_namespace(self, user_id: UUID, namespace: IdentityNamespace) -> bool:
        return (
            self.session.scalar(
                select(ExternalIdentity.id)
                .where(
                    ExternalIdentity.user_id == str(user_id),
                    ExternalIdentity.provider == namespace.provider,
                    ExternalIdentity.namespace == namespace.namespace,
                )
                .limit(1)
            )
            is not None
        )

    def privileged_roles(self, permissions: tuple[str, ...]) -> frozenset[str]:
        return frozenset(
            self.session.scalars(
                select(Role.name)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(Permission, Permission.id == RolePermission.permission_id)
                .where(Permission.name.in_(permissions))
                .distinct()
            )
        )

    def user(self, user_id: UUID) -> User | None:
        return self.session.get(User, str(user_id), populate_existing=True)
