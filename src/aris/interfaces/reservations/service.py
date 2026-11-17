from collections.abc import Sequence

from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import GatewayLimits
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserCommandAuthenticator
from aris.modules.nova.reservation_schemas import MineQuery, MineResult
from aris.modules.nova.reservation_service import ReservationService


class UserReservationSurface:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        reservations: ReservationService,
        namespace: IdentityNamespace,
        authentication: UserCommandAuthenticator,
        limits: GatewayLimits,
    ) -> None:
        self.sessions, self.reservations = sessions, reservations
        self.namespace, self.authentication, self.limits = namespace, authentication, limits
        self.access = AccessService(sessions)

    def validate_configuration(
        self, environment: str, reserved_tokens: Sequence[SecretStr]
    ) -> None:
        if environment == "production" and self.authentication.development_only:
            raise ValueError("development user credentials cannot enable production queries")
        self.authentication.ensure_distinct_credentials(reserved_tokens)

    def mine(self, authorization: str | None, request_id: str, query: MineQuery) -> MineResult:
        subject = self.authentication.authenticate(authorization)
        if (subject.provider, subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "User namespace differs", 403)
        try:
            # This owned session is closed without commit. Identity resolution,
            # current grants and the owner-filtered query use one transaction.
            with self.sessions() as session:
                transaction_budget(
                    session,
                    self.limits.lock_timeout_ms,
                    self.limits.statement_timeout_ms,
                    self.limits.transaction_timeout_ms,
                )
                principal = self.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise DomainError("PERMISSION_DENIED", "User identity is not registered", 403)
                return self.reservations.mine_in_transaction(
                    session, ActorContext(actor_id=principal.user_id, request_id=request_id), query
                )
        except DBAPIError:
            raise DomainError(
                "QUERY_UNAVAILABLE", "Personal reservations are unavailable", 503
            ) from None
