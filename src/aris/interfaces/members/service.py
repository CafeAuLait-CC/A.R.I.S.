"""The member portal composes public identity, usage and hardware services."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aris.core.access.directory import AccessDirectoryQueries
from aris.core.access.directory_schemas import DirectoryPage, DirectoryQuery
from aris.core.access.member_schemas import MemberAccessSummary, PortalIdentity
from aris.core.access.members import MemberAdministrationService
from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope
from aris.core.access.sessions import LoginSessionService, SessionBearerAuthenticator
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.identity.service import IdentityAdminSurface, IdentityRosterPage
from aris.interfaces.identity.slack_directory import SlackRosterQuery
from aris.interfaces.usage.service import CalendarWindow, UsageHistorySurface, UTCWindow
from aris.modules.lyra.general_usage import GeneralUsageService, GeneralUsageTotal
from aris.modules.lyra.usage_history import UsageHistoryQuery, calendar_boundaries
from aris.modules.vega.service import VegaService


class MemberDirectory(AccessContract):
    directory: DirectoryPage
    access: tuple[MemberAccessSummary, ...]


class MemberRoster(AccessContract):
    roster: IdentityRosterPage
    access: tuple[MemberAccessSummary, ...]


class GeneralQuery(AccessContract):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    window: Annotated[CalendarWindow | UTCWindow, Field(discriminator="kind")]


class GeneralGpu(AccessContract):
    index: int
    name: str
    state: Literal["idle", "busy", "unknown"]
    memory_used_mib: int | None
    memory_total_mib: int | None
    utilization_percent: float | None
    temperature_c: float | None


class GeneralNode(AccessContract):
    node_id: str
    status: str
    telemetry_status: str
    observed_at: AwareDatetime | None = None
    cpu_percent: float | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    gpus: tuple[GeneralGpu, ...] = ()


class GeneralOverview(AccessContract):
    hardware: GeneralNode
    recorded_usage: GeneralUsageTotal


class MemberPortalSurface:
    def __init__(
        self,
        members: MemberAdministrationService,
        authenticator: SessionBearerAuthenticator,
        commands: UserCommandSurface,
        usage: UsageHistorySurface,
        general_usage: GeneralUsageService,
        hardware: VegaService,
        node_ids: tuple[str, ...],
        *,
        origin: str,
        roster: IdentityAdminSurface | None = None,
    ) -> None:
        self.members, self.authenticator, self.commands = members, authenticator, commands
        self.login: LoginSessionService = authenticator.service
        self.directory = AccessDirectoryQueries(members.administration)
        self.usage, self.general_usage, self.hardware = usage, general_usage, hardware
        self.node_ids, self.origin, self.roster_source = node_ids, origin, roster

    def identity(self, authorization: str | None) -> PortalIdentity:
        return self.members.identity(self.authenticator.authenticate(authorization))

    def read_directory(
        self, authorization: str | None, after: str | None, limit: int
    ) -> MemberDirectory:
        actor = self.identity(authorization)
        page = self.directory.page(
            actor.user_id, DirectoryQuery(node_ids=self.node_ids, after=after, limit=limit)
        )
        summary = self.members.summaries(
            actor.user_id, tuple(member.user_id for member in page.members)
        )
        if self.identity(authorization).user_id != actor.user_id:
            raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
        return MemberDirectory(directory=page, access=summary)

    def roster(
        self, authorization: str | None, request_id: str, query: SlackRosterQuery
    ) -> MemberRoster:
        if self.roster_source is None:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Slack member loading is not configured", 503
            )
        actor = self.identity(authorization)
        page = self.roster_source.roster(authorization, request_id, query)
        summary = self.members.summaries(
            actor.user_id,
            tuple(member.registered.user_id for member in page.members if member.registered),
        )
        if self.identity(authorization).user_id != actor.user_id:
            raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
        return MemberRoster(roster=page, access=summary)

    def general(
        self, authorization: str | None, request_id: str, query: GeneralQuery
    ) -> GeneralOverview:
        if query.node_id not in self.node_ids:
            raise DomainError("PERMISSION_DENIED", "Node is outside this portal", 403)
        actor = self.identity(authorization)
        request = AccessRequest(
            user_id=actor.user_id,
            permission="usage.read_general",
            scope=AccessScope(type="node", id=query.node_id),
        )
        self.members.access.require_permission(request)
        if isinstance(query.window, CalendarWindow):
            bounds = calendar_boundaries(
                query.window.start_date, query.window.end_date, query.window.timezone
            )
            start, end = bounds[0], min(bounds[-1], self.general_usage._now())
        else:
            start, end = query.window.start_at, query.window.end_at
        try:
            window = UsageHistoryQuery(
                node_id=query.node_id, start_at=start, end_at=end, timezone=query.window.timezone
            )
        except ValueError:
            raise DomainError("VALIDATION_ERROR", "Select a valid past usage window", 422) from None
        total = self.general_usage.total(
            ActorContext(actor_id=actor.user_id, request_id=request_id),
            window,
        )
        item = next(
            (
                item
                for item in self.hardware.resource_status().nodes
                if item.node.node_id == query.node_id
            ),
            None,
        )
        current = GeneralNode(node_id=query.node_id, status="UNKNOWN", telemetry_status="MISSING")
        if item is not None:
            node = item.node
            fresh = node.status == "ONLINE" and node.telemetry_status == "CURRENT"
            system = (
                item.system
                if node.status == "ONLINE" and item.system_freshness == "CURRENT"
                else None
            )
            memory = system.memory if system is not None and system.memory.quality == "ok" else None
            current = GeneralNode(
                node_id=node.node_id,
                status=node.status,
                telemetry_status=node.telemetry_status,
                observed_at=node.observed_at,
                cpu_percent=system.cpu.utilization_percent
                if system is not None and system.cpu.quality == "ok"
                else None,
                memory_total_bytes=memory.total_bytes if memory else None,
                memory_available_bytes=memory.available_bytes if memory else None,
                gpus=tuple(
                    GeneralGpu(
                        index=gpu.index,
                        name=gpu.model,
                        state=gpu.observed_state if fresh and gpu.present else "unknown",
                        memory_used_mib=gpu.memory_used_mib if fresh and gpu.present else None,
                        memory_total_mib=gpu.memory_total_mib if fresh and gpu.present else None,
                        utilization_percent=gpu.utilization_percent
                        if fresh and gpu.present
                        else None,
                        temperature_c=gpu.temperature_c if fresh and gpu.present else None,
                    )
                    for gpu in node.gpus
                ),
            )
        self.members.access.require_permission(request)
        if self.identity(authorization).user_id != UUID(str(actor.user_id)):
            raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
        return GeneralOverview(hardware=current, recorded_usage=total)
