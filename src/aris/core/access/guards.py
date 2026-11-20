"""Optional application permission ceilings, independent of concrete domains."""

from collections.abc import Mapping

from pydantic import TypeAdapter
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, PermissionName, UserPrincipal
from aris.core.access.service import AccessService


class GuardedAccessService(AccessService):
    def __init__(self, sessions: sessionmaker[Session], global_guards: Mapping[str, str]) -> None:
        super().__init__(sessions)
        permission = TypeAdapter(PermissionName)
        self.guards = {
            permission.validate_python(requested): permission.validate_python(required)
            for requested, required in global_guards.items()
        }

    def require_in_transaction(self, session: Session, request: AccessRequest) -> UserPrincipal:
        if required := self.guards.get(request.permission):
            super().require_in_transaction(
                session,
                AccessRequest(
                    user_id=request.user_id,
                    permission=required,
                    scope=AccessScope(type="global", id="*"),
                ),
            )
        return super().require_in_transaction(session, request)
