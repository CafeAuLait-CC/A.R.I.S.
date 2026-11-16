from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.models import User
from aris.core.access.repo import AccessRepository
from aris.core.access.schemas import AccessRequest, ExternalSubject, NodeUserLookup, UserPrincipal
from aris.core.contracts import DomainError


class AccessService:
    """Read-only identity resolution and current grants, without implicit enrollment.

    A resolved principal is not a permission ticket. Future write services must
    revalidate access in the same transaction as their protected business work.
    """

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _principal(user: User | None) -> UserPrincipal | None:
        if user is None:
            return None
        try:
            return UserPrincipal(
                user_id=user.id, display_name=user.display_name, timezone=user.timezone
            )
        except ValidationError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Identity configuration is invalid", 503
            ) from None

    def resolve_external(self, subject: ExternalSubject) -> UserPrincipal | None:
        with self.sessions() as session:
            return self._principal(AccessRepository(session).external_user(subject))

    def resolve_node_user(self, lookup: NodeUserLookup) -> UserPrincipal | None:
        with self.sessions() as session:
            return self._principal(AccessRepository(session).mapped_node_user(lookup))

    def require_permission(self, request: AccessRequest) -> UserPrincipal:
        with self.sessions() as session:
            user = self._principal(AccessRepository(session).permitted_user(request))
        if user is None:
            raise DomainError("PERMISSION_DENIED", "Permission is not granted", 403)
        return user
