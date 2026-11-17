from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import ExternalCommandActor, GatewayLimits
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserCommandAuthenticator
from aris.modules.nova.availability_schemas import AvailabilityQuery, AvailabilityResult
from aris.modules.nova.reservation_schemas import MineQuery, MineResult
from aris.modules.nova.reservation_service import ReservationService


class PersonalReservationQueries:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        reservations: ReservationService,
        namespace: IdentityNamespace,
        limits: GatewayLimits,
    ) -> None:
        self.sessions, self.reservations = sessions, reservations
        self.namespace, self.limits = namespace, limits
        self.access = AccessService(sessions)

    @contextmanager
    def _query_session(
        self, context: ExternalCommandActor
    ) -> Iterator[tuple[Session, UserPrincipal]]:
        # The interface constructs this context only after authenticating its
        # provider; it is not an HTTP body or permission ticket.
        subject = context.subject
        if (subject.provider, subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "User namespace differs", 403)
        try:
            # The owned query session closes without commit; identity, current
            # grants and domain reads use this same bounded transaction.
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
                yield session, principal
        except DBAPIError:
            raise DomainError(
                "QUERY_UNAVAILABLE", "User reservation query is unavailable", 503
            ) from None

    def mine(self, context: ExternalCommandActor, query: MineQuery) -> MineResult:
        with self._query_session(context) as (session, principal):
            return self.reservations.mine_in_transaction(
                session,
                ActorContext(actor_id=principal.user_id, request_id=context.request_id),
                query,
            )

    def availability(
        self, context: ExternalCommandActor, query: AvailabilityQuery
    ) -> AvailabilityResult:
        with self._query_session(context) as (session, principal):
            scope = self.reservations.target_scope(session, query.gpu_uuid)
            self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=principal.user_id, permission="reservation.create", scope=scope
                ),
            )
            result = self.reservations.availability.assess_in_transaction(session, query)
            if result.resource is not None:
                # A read can see a later durable resource projection. Never
                # disclose its window assessment under an earlier node grant.
                self.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=principal.user_id,
                        permission="reservation.create",
                        scope=AccessScope(type="node", id=result.resource.node_id),
                    ),
                )
            return result


class UserReservationSurface:
    def __init__(
        self, queries: PersonalReservationQueries, authentication: UserCommandAuthenticator
    ) -> None:
        self.queries, self.authentication = queries, authentication

    def validate_configuration(
        self, environment: str, reserved_tokens: Sequence[SecretStr]
    ) -> None:
        if environment == "production" and self.authentication.development_only:
            raise ValueError("development user credentials cannot enable production queries")
        self.authentication.ensure_distinct_credentials(reserved_tokens)

    def mine(self, authorization: str | None, request_id: str, query: MineQuery) -> MineResult:
        return self.queries.mine(
            self._context(authorization, request_id),
            query,
        )

    def availability(
        self, authorization: str | None, request_id: str, query: AvailabilityQuery
    ) -> AvailabilityResult:
        return self.queries.availability(self._context(authorization, request_id), query)

    def _context(self, authorization: str | None, request_id: str) -> ExternalCommandActor:
        return ExternalCommandActor(
            subject=self.authentication.authenticate(authorization), request_id=request_id
        )
