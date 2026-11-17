from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserCommandAuthenticator
from aris.interfaces.reservations.schemas import (
    LabeledMinePage,
    OwnedReservationDetail,
    ReservationQueryLimits,
)
from aris.modules.nova.availability_schemas import AvailabilityQuery, AvailabilityResult
from aris.modules.nova.reservation_schemas import MineQuery, MineResult
from aris.modules.nova.reservation_service import ReservationService
from aris.modules.vega.target_schemas import (
    GpuCatalogPage,
    GpuCatalogQuery,
    GpuInventoryLabel,
    GpuLabelQuery,
)


class GpuLabels(Protocol):
    def labels_in_transaction(
        self, session: Session, query: GpuLabelQuery
    ) -> tuple[GpuInventoryLabel, ...]: ...

    def catalog_in_transaction(
        self, session: Session, query: GpuCatalogQuery
    ) -> GpuCatalogPage: ...


class PersonalReservationQueries:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        reservations: ReservationService,
        namespace: IdentityNamespace,
        limits: ReservationQueryLimits,
        labels: GpuLabels | None = None,
    ) -> None:
        self.sessions, self.reservations = sessions, reservations
        self.namespace, self.limits = namespace, limits
        self.access = AccessService(sessions)
        self.labels = labels

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
            result = self.reservations.mine_in_transaction(
                session,
                ActorContext(actor_id=principal.user_id, request_id=context.request_id),
                query,
            )
            self._recheck_mine(session, context, principal.user_id)
            return result

    def mine_labeled(self, context: ExternalCommandActor, query: MineQuery) -> LabeledMinePage:
        with self._query_session(context) as (session, principal):
            result = self.reservations.mine_in_transaction(
                session,
                ActorContext(actor_id=principal.user_id, request_id=context.request_id),
                query,
            )
            targets = tuple(dict.fromkeys(view.gpu_uuid for view in result.reservations))
            labels = (
                self.labels.labels_in_transaction(session, GpuLabelQuery(gpu_uuids=targets))
                if targets and self.labels
                else ()
            )
            self._recheck_mine(session, context, principal.user_id)
            return LabeledMinePage(page=result, labels=labels)

    def detail(self, context: ExternalCommandActor, reservation_id: UUID) -> OwnedReservationDetail:
        with self._query_session(context) as (session, principal):
            view = self.reservations.owned_in_transaction(
                session,
                ActorContext(actor_id=principal.user_id, request_id=context.request_id),
                reservation_id,
            )
            labels = (
                self.labels.labels_in_transaction(
                    session, GpuLabelQuery(gpu_uuids=(view.gpu_uuid,))
                )
                if self.labels
                else ()
            )
            if len(labels) > 1:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Reservation labels are ambiguous", 503)
            self._recheck_mine(session, context, principal.user_id)
            return OwnedReservationDetail(
                reservation=view, resource_label=labels[0] if labels else None
            )

    def _recheck_mine(self, session: Session, context: ExternalCommandActor, user_id: UUID) -> None:
        self._recheck_identity(session, context, user_id)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=user_id,
                permission="reservation.mine",
                scope=AccessScope(type="global", id="*"),
            ),
        )

    def _recheck_identity(
        self, session: Session, context: ExternalCommandActor, user_id: UUID
    ) -> None:
        current = self.access.resolve_external_in_transaction(session, context.subject)
        if current is None or current.user_id != user_id:
            raise DomainError("PERMISSION_DENIED", "Current user identity differs", 403)

    def create_choices(self, context: ExternalCommandActor) -> GpuCatalogPage:
        if self.labels is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Resource labels are unavailable", 503)
        with self._query_session(context) as (session, principal):
            page = self.labels.catalog_in_transaction(session, GpuCatalogQuery())
            nodes = {label.node_id for label in page.labels}
            allowed = self._create_nodes(session, principal.user_id, nodes)
            selected = tuple(label for label in page.labels if label.node_id in allowed)
            final = (
                self.labels.labels_in_transaction(
                    session, GpuLabelQuery(gpu_uuids=tuple(label.gpu_uuid for label in selected))
                )
                if selected
                else ()
            )
            stable = {(label.gpu_uuid, label.node_id) for label in final}
            self._recheck_identity(session, context, principal.user_id)
            allowed = self._create_nodes(session, principal.user_id, allowed)
            return GpuCatalogPage(
                labels=tuple(
                    label
                    for label in selected
                    if (label.gpu_uuid, label.node_id) in stable and label.node_id in allowed
                ),
                truncated=page.truncated,
            )

    def _create_nodes(self, session: Session, user_id: UUID, nodes: set[str]) -> set[str]:
        allowed = set()
        for node in sorted(nodes):
            try:
                self.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=user_id,
                        permission="reservation.create",
                        scope=AccessScope(type="node", id=node),
                    ),
                )
                allowed.add(node)
            except DomainError as error:
                if error.code != "PERMISSION_DENIED":
                    raise
        return allowed

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

    def detail(
        self, authorization: str | None, request_id: str, reservation_id: UUID
    ) -> OwnedReservationDetail:
        return self.queries.detail(self._context(authorization, request_id), reservation_id)

    def _context(self, authorization: str | None, request_id: str) -> ExternalCommandActor:
        return ExternalCommandActor(
            subject=self.authentication.authenticate(authorization), request_id=request_id
        )
