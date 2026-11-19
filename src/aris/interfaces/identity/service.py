from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.exc import DBAPIError

from aris.core.access.directory import AccessDirectoryQueries
from aris.core.access.directory_schemas import DirectoryMember, DirectoryPage, DirectoryQuery
from aris.core.access.schemas import AccessContract
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.contracts import DomainError
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.identity.node_observations import (
    NodeUserObservationProvider,
    NodeUserObservations,
)
from aris.interfaces.identity.slack_directory import (
    SlackDirectoryProvider,
    SlackRosterMember,
    SlackRosterQuery,
)
from aris.interfaces.slack.config import SlackSettings


class DirectoryPageInput(AccessContract):
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    after: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^\S+$")


class IdentityQueryLimits(AccessContract):
    lock_timeout_ms: int = Field(default=100, gt=0, le=1000, strict=True)
    statement_timeout_ms: int = Field(default=1000, gt=0, le=5000, strict=True)
    transaction_timeout_ms: int = Field(default=3000, gt=0, le=10000, strict=True)

    @model_validator(mode="after")
    def ordered_budgets(self) -> "IdentityQueryLimits":
        if not self.lock_timeout_ms < self.statement_timeout_ms < self.transaction_timeout_ms:
            raise ValueError("identity query budgets must increase")
        return self


class IdentityRosterMember(AccessContract):
    slack: SlackRosterMember
    registered: DirectoryMember | None


class IdentityRosterPage(AccessContract):
    namespace: str
    node_ids: tuple[str, ...]
    revision: int = Field(ge=1)
    slack_fetched_at: AwareDatetime
    members: tuple[IdentityRosterMember, ...] = Field(max_length=50)
    next_cursor: str | None = Field(default=None, max_length=2048, repr=False)


class IdentityAdminSurface:
    def __init__(
        self,
        commands: UserCommandSurface,
        directory: AccessDirectoryQueries,
        provider: SlackDirectoryProvider,
        node_ids: tuple[str, ...],
        limits: IdentityQueryLimits,
        observations: NodeUserObservationProvider | None = None,
    ) -> None:
        DirectoryQuery(node_ids=node_ids)
        if directory.administration.namespace.provider != "slack":
            raise ValueError("identity roster requires the Slack namespace")
        if not commands.gateway.definition("identity.map_users").requires_confirmation:
            raise ValueError("identity mapping requires confirmation")
        self.commands, self.directory, self.provider = commands, directory, provider
        self.node_ids, self.limits = node_ids, limits
        self.observations = observations

    def validate_workspace(self, settings: SlackSettings) -> None:
        if (
            not settings.configured
            or settings.team_id != self.directory.administration.namespace.namespace
        ):
            raise ValueError("identity roster requires the configured Slack workspace")

    def _page(
        self,
        context: ExternalCommandActor,
        query: DirectoryQuery,
        expected_actor: UUID | None = None,
    ) -> tuple[UUID, DirectoryPage]:
        administration = self.directory.administration
        if (context.subject.provider, context.subject.namespace) != (
            administration.namespace.provider,
            administration.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "Identity namespace differs", 403)
        try:
            with administration.sessions() as session:
                transaction_budget(
                    session,
                    self.limits.lock_timeout_ms,
                    self.limits.statement_timeout_ms,
                    self.limits.transaction_timeout_ms,
                )
                principal = administration.access.resolve_external_in_transaction(
                    session, context.subject
                )
                if principal is None or (
                    expected_actor is not None and principal.user_id != expected_actor
                ):
                    raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
                page = self.directory.page_in_transaction(session, principal.user_id, query)
                current = administration.access.resolve_external_in_transaction(
                    session, context.subject
                )
                if current is None or current.user_id != principal.user_id:
                    raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
                return principal.user_id, page
        except DBAPIError:
            raise DomainError(
                "QUERY_UNAVAILABLE", "Identity directory is unavailable", 503
            ) from None

    def read_directory(
        self, authorization: str | None, request_id: str, query: DirectoryPageInput
    ) -> DirectoryPage:
        context = self.commands.actor(authorization, request_id)
        return self._page(
            context, DirectoryQuery(node_ids=self.node_ids, limit=query.limit, after=query.after)
        )[1]

    def node_users(self, authorization: str | None, request_id: str) -> NodeUserObservations:
        context = self.commands.actor(authorization, request_id)
        query = DirectoryQuery(node_ids=self.node_ids, external_ids=())
        actor_id, _ = self._page(context, query)
        if self.observations is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Node observations are unavailable", 503)
        try:
            result = self.observations.read(self.node_ids)
            result = NodeUserObservations.model_validate(result.model_dump(mode="json"))
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Node observations are unavailable", 503
            ) from None
        if tuple(value.node_id for value in result.nodes) != self.node_ids:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Node observation scope differs", 503)
        self._page(context, query, expected_actor=actor_id)
        return result

    def roster(
        self, authorization: str | None, request_id: str, query: SlackRosterQuery
    ) -> IdentityRosterPage:
        context = self.commands.actor(authorization, request_id)
        actor_id, _ = self._page(context, DirectoryQuery(node_ids=self.node_ids, external_ids=()))
        # The authorization transaction has closed before any network request.
        try:
            roster = self.provider.page(query)
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "SLACK_DIRECTORY_UNAVAILABLE", "Slack directory is unavailable", 503
            ) from None
        namespace = self.directory.administration.namespace.namespace
        if roster.team_id != namespace:
            raise DomainError("SLACK_DIRECTORY_UNAVAILABLE", "Slack workspace differs", 503)
        _, page = self._page(
            context,
            DirectoryQuery(
                node_ids=self.node_ids,
                limit=50,
                external_ids=tuple(member.external_id for member in roster.members),
            ),
            expected_actor=actor_id,
        )
        members = {member.external_id: member for member in page.members}
        return IdentityRosterPage(
            namespace=namespace,
            node_ids=self.node_ids,
            revision=page.revision,
            slack_fetched_at=roster.fetched_at,
            next_cursor=roster.next_cursor,
            members=tuple(
                IdentityRosterMember(slack=person, registered=members.get(person.external_id))
                for person in roster.members
            ),
        )
