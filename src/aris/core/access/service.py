from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.models import User
from aris.core.access.repo import AccessRepository
from aris.core.access.schemas import (
    AccessRequest,
    AuthorizedUsersQuery,
    ExternalSubject,
    ExternalUserLookup,
    ExternalUserTarget,
    NodeUserLookup,
    UserLabel,
    UserLabelQuery,
    UserPrincipal,
)
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
            return self.resolve_external_in_transaction(session, subject)

    def labels_in_transaction(
        self, session: Session, query: UserLabelQuery
    ) -> tuple[UserLabel, ...]:
        """Bounded labels, not identity resolution or an authorization decision."""
        query = UserLabelQuery.model_validate(query.model_dump(mode="json"))
        with session.no_autoflush:
            return tuple(
                UserLabel(user_id=user.id, display_name=user.display_name)
                for user in AccessRepository(session).user_labels(query)
            )

    def resolve_external_in_transaction(
        self, session: Session, subject: ExternalSubject
    ) -> UserPrincipal | None:
        with session.no_autoflush:
            return self._principal(AccessRepository(session).external_user(subject))

    def resolve_node_user(self, lookup: NodeUserLookup) -> UserPrincipal | None:
        with self.sessions() as session:
            return self._principal(AccessRepository(session).mapped_node_user(lookup))

    def external_target_in_transaction(
        self, session: Session, lookup: ExternalUserLookup
    ) -> ExternalUserTarget | None:
        with session.no_autoflush:
            identities = AccessRepository(session).external_targets(lookup)
        if not identities:
            return None
        if len(identities) != 1:
            raise DomainError("IDENTITY_AMBIGUOUS", "External recipient is ambiguous", 503)
        identity = identities[0]
        return ExternalUserTarget(
            user_id=lookup.user_id,
            subject=ExternalSubject(
                provider=identity.provider,
                namespace=identity.namespace,
                external_id=identity.external_id,
            ),
        )

    def require_permission(self, request: AccessRequest) -> UserPrincipal:
        with self.sessions() as session:
            return self.require_in_transaction(session, request)

    def require_in_transaction(self, session: Session, request: AccessRequest) -> UserPrincipal:
        """Check current grants in a transaction owned by a platform service."""
        user = self._principal(AccessRepository(session).permitted_user(request))
        if user is None:
            raise DomainError("PERMISSION_DENIED", "Permission is not granted", 403)
        return user

    def permitted_users_in_transaction(
        self, session: Session, query: AuthorizedUsersQuery
    ) -> tuple[UserPrincipal, ...]:
        """One bounded SQL query, no implicit grants or cached authorization ticket.

        Domain services own the transaction and still authorize their requester.
        Namespace ambiguity is a dependency error, not a silently missing user.
        """
        query = AuthorizedUsersQuery.model_validate(query.model_dump(mode="json"))
        if not query.user_ids:
            return ()
        with session.no_autoflush:
            rows = AccessRepository(session).permitted_users(query)
            if any(count != 1 for *_, count in rows):
                raise DomainError("IDENTITY_AMBIGUOUS", "External user identity is ambiguous", 503)
            try:
                principals = {
                    id: UserPrincipal(user_id=id, display_name=name, timezone=zone)
                    for id, name, zone, _ in rows
                }
            except ValidationError:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Identity configuration is invalid", 503
                ) from None
            return tuple(
                value for id in query.user_ids if (value := principals.get(str(id))) is not None
            )
