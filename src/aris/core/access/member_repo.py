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
        rows = self.session.execute(
            select(Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
            .join(Role, Role.id == RoleBinding.role_id)
            .where(RoleBinding.user_id == user_id)
            .order_by(Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
            .limit(33)
        )
        return tuple(
            ExistingRoleBinding(role_name=row[0], scope=AccessScope(type=row[1], id=row[2]))
            for row in rows
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
