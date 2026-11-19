"""Read-only adapter for stored policy sources, not a policy publication surface."""

from collections.abc import Callable
from time import monotonic
from typing import TypeVar

from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract, AccessRequest, UserPrincipal
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import BoundSubjectTokens
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
    PolicySourcePage,
    PolicySourceReference,
    PolicySourceView,
    StoredPolicyComparison,
)
from aris.modules.nova.policy_catalog_service import (
    DRAFT_PERMISSION,
    GLOBAL_SCOPE,
    PolicyCatalogService,
)
from aris.modules.nova.policy_schemas import PolicyName

Result = TypeVar("Result", bound=BaseModel)


class PolicyCatalogSession(AccessContract):
    actor: UserPrincipal
    policy_names: tuple[PolicyName, ...] = Field(min_length=1, max_length=32)


class PolicyDetailInput(AccessContract):
    reference: PolicySourceReference
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class PolicyReadSurface:
    def __init__(
        self,
        catalog: PolicyCatalogService,
        authenticator: BoundSubjectTokens,
        policy_names: tuple[str, ...],
    ) -> None:
        if not 1 <= len(policy_names) <= 32 or len(set(policy_names)) != len(policy_names):
            raise ValueError("policy interface requires explicit unique policy names")
        for name in policy_names:
            PolicySourceReference(name=name, version=1)
        self.catalog, self.authenticator, self.policy_names = catalog, authenticator, policy_names

    def _run(
        self,
        authorization: str | None,
        request_id: str,
        operation: Callable[[Session, ActorContext, UserPrincipal], Result],
        *,
        names: tuple[str, ...] = (),
    ) -> Result:
        subject = self.authenticator.authenticate(authorization)
        deadline = monotonic() + 3
        try:
            with self.catalog.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                principal = self.catalog.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
                actor = ActorContext(actor_id=principal.user_id, request_id=request_id)
                access = AccessRequest(
                    user_id=actor.actor_id, permission=DRAFT_PERMISSION, scope=GLOBAL_SCOPE
                )
                self.catalog.access.require_in_transaction(session, access)
                for name in names:
                    self._name(name)
                result = operation(session, actor, principal)
                self.catalog.access.require_in_transaction(session, access)
                current = self.catalog.access.resolve_external_in_transaction(session, subject)
                if current is None or current.user_id != principal.user_id:
                    raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Policy read budget exceeded", 503
                    )
                return result
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Policy database is unavailable", 503
            ) from None

    def session(self, authorization: str | None, request_id: str) -> PolicyCatalogSession:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: PolicyCatalogSession(
                actor=principal, policy_names=self.policy_names
            ),
        )

    def _name(self, name: str) -> None:
        if name not in self.policy_names:
            raise DomainError("PERMISSION_DENIED", "Policy name is outside this interface", 403)

    def sources(
        self, authorization: str | None, request_id: str, query: PolicySourceListQuery
    ) -> PolicySourcePage:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: self.catalog.sources_in_transaction(
                session, actor, query
            ),
            names=(query.name,),
        )

    def detail(
        self, authorization: str | None, request_id: str, query: PolicyDetailInput
    ) -> PolicySourceView:
        def read(
            session: Session, actor: ActorContext, principal: UserPrincipal
        ) -> PolicySourceView:
            view = self.catalog.exact_in_transaction(session, actor, query.reference)
            if view.fingerprint != query.expected_fingerprint:
                raise DomainError("CONFLICT", "Stored policy source fingerprint differs", 409)
            return view

        return self._run(authorization, request_id, read, names=(query.reference.name,))

    def compare(
        self, authorization: str | None, request_id: str, query: PolicySourceComparisonQuery
    ) -> StoredPolicyComparison:
        return self._run(
            authorization,
            request_id,
            lambda session, actor, principal: self.catalog.compare_sources_in_transaction(
                session, actor, query
            ),
            names=(query.before.name, query.after.name),
        )
