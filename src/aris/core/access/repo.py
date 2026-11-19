from collections.abc import Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from aris.core.access.models import (
    ExternalIdentity,
    NodeUserMapping,
    Permission,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import (
    AccessRequest,
    ExternalSubject,
    ExternalUserLookup,
    NodeUserLookup,
    UserLabelQuery,
)


class AccessRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def user_labels(self, query: UserLabelQuery) -> Sequence[User]:
        # Historical owners keep a display label even after their account is disabled.
        return self.session.scalars(
            select(User)
            .where(User.id.in_([str(value) for value in query.user_ids]))
            .execution_options(populate_existing=True)
        ).all()

    def external_targets(self, lookup: ExternalUserLookup) -> Sequence[ExternalIdentity]:
        return self.session.scalars(
            select(ExternalIdentity)
            .join(User, User.id == ExternalIdentity.user_id)
            .where(
                User.id == str(lookup.user_id),
                User.status == "active",
                ExternalIdentity.provider == lookup.provider,
                ExternalIdentity.namespace == lookup.namespace,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        ).all()

    def external_user(self, subject: ExternalSubject) -> User | None:
        return self.session.scalar(
            select(User)
            .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
            .where(
                User.status == "active",
                ExternalIdentity.provider == subject.provider,
                ExternalIdentity.namespace == subject.namespace,
                ExternalIdentity.external_id == subject.external_id,
            )
        )

    def mapped_node_user(self, lookup: NodeUserLookup) -> User | None:
        return self.session.scalar(
            select(User)
            .join(NodeUserMapping, NodeUserMapping.user_id == User.id)
            .where(
                User.status == "active",
                NodeUserMapping.node_id == lookup.node_id,
                NodeUserMapping.os_username == lookup.os_username,
                NodeUserMapping.confidence == "verified",
                NodeUserMapping.source == "manual",
            )
        )

    def permitted_user(self, request: AccessRequest) -> User | None:
        return self.session.scalar(
            select(User)
            .join(RoleBinding, RoleBinding.user_id == User.id)
            .join(RolePermission, RolePermission.role_id == RoleBinding.role_id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                User.id == str(request.user_id),
                User.status == "active",
                Permission.name == request.permission,
                or_(
                    and_(RoleBinding.scope_type == "global", RoleBinding.scope_id == "*"),
                    and_(
                        RoleBinding.scope_type == request.scope.type,
                        RoleBinding.scope_id == request.scope.id,
                    ),
                ),
            )
            .limit(1)
        )
