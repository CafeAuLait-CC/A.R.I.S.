from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessScope
from aris.modules.vega.repo import VegaRepository


class VegaNodeScopes:
    """Public node registration catalog; existence does not imply online health."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def known(self, scope: AccessScope) -> bool:
        with self.sessions() as session:
            return self.known_in_transaction(session, scope)

    def known_in_transaction(self, session: Session, scope: AccessScope) -> bool:
        if scope.type != "node":
            return False
        with session.no_autoflush:
            return VegaRepository(session).node_exists(scope.id)
