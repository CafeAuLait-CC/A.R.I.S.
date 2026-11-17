from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from aris.core.access.admin_repo import MANAGE_IDENTITIES, AccessAdministrationRepository
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.directory_schemas import DirectoryMember, DirectoryPage, DirectoryQuery
from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.contracts import DomainError


class AccessDirectoryQueries:
    """Read-only catalog for a trusted current identity administrator."""

    def __init__(self, administration: AccessAdministrationService) -> None:
        self.administration = administration

    def _authority(self, session: Session, actor_id: UUID) -> None:
        service = self.administration
        service.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=actor_id,
                permission=MANAGE_IDENTITIES,
                scope=AccessScope(type="global", id="*"),
            ),
        )
        if not AccessAdministrationRepository(session).has_login_identity(
            str(actor_id), service.namespace
        ):
            raise DomainError("PERMISSION_DENIED", "Identity namespace is not linked", 403)

    def page(self, actor_id: UUID, query: DirectoryQuery) -> DirectoryPage:
        with self.administration.sessions() as session:
            return self.page_in_transaction(session, actor_id, query)

    def page_in_transaction(
        self, session: Session, actor_id: UUID, query: DirectoryQuery
    ) -> DirectoryPage:
        try:
            with session.no_autoflush:
                self._authority(session, actor_id)
                service = self.administration
                repo = AccessAdministrationRepository(session)
                state = repo.directory_snapshot()
                if state is None or not state[3]:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Identity directory is not initialized", 503
                    )
                if state[:2] != (service.namespace.provider, service.namespace.namespace):
                    raise DomainError("CONFLICT", "Directory namespace differs", 409)
                for node in query.node_ids:
                    service._scope(session, AccessScope(type="node", id=node))
                fetched = repo.directory_members(
                    service.namespace, query.after, query.limit, query.external_ids
                )
                members = fetched[: query.limit]
                mappings = (
                    repo.directory_mappings(
                        tuple(str(member.user_id) for member in members), query.node_ids
                    )
                    if members
                    else ()
                )
                if len(mappings) > 800:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Directory mappings exceed the page budget", 503
                    )
                result = DirectoryPage(
                    namespace=service.namespace,
                    revision=state[2],
                    node_ids=query.node_ids,
                    members=tuple(
                        DirectoryMember(
                            user_id=member.user_id,
                            external_id=member.external_id,
                            profile=member.profile,
                            status=member.status,
                            mappings=tuple(
                                mapping for mapping in mappings if mapping.user_id == member.user_id
                            ),
                        )
                        for member in members
                    ),
                    next_cursor=members[-1].external_id if len(fetched) > query.limit else None,
                )
                if repo.directory_snapshot() != state:
                    raise DomainError(
                        "CONFLICT", "Identity directory changed; refresh the page", 409
                    )
                self._authority(session, actor_id)
                return result
        except ValidationError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Identity directory data is invalid", 503
            ) from None
