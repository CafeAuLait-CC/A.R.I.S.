"""Opt-in member portal; construction never changes users, grants or schemas."""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import DefineRole, IdentityNamespace, RegistrationRoleGrant
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.directory import AccessDirectoryQueries
from aris.core.access.guards import GuardedAccessService
from aris.core.access.member_commands import EditOwnProfileCommand, ManageMembersCommand
from aris.core.access.members import MemberAdministrationService
from aris.core.access.passwords import InvitationSender, PasswordAccountService
from aris.core.access.schemas import AccessContract, AccessScope, Identifier
from aris.core.access.sessions import LoginSessionService, SessionBearerAuthenticator, secret_digest
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import GatewayLimits
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.queue_schemas import CommandWorkerLimits, IntakeLimits
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from aris.core.config import Settings
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.identity.service import IdentityAdminSurface, IdentityQueryLimits
from aris.interfaces.identity.slack_directory import SlackDirectoryClient, SlackDirectoryProvider
from aris.interfaces.members.service import MemberPortalSurface
from aris.interfaces.slack.client import SlackClient
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.invitations import SlackInvitationSender
from aris.interfaces.usage.activity_service import ActivityPreviewAccess, ActivityPreviewConfig
from aris.interfaces.usage.service import UsageHistorySurface
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.general_usage import GeneralUsageService
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.weekly_report_service import WeeklyReportReader
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.coverage_schedule_reader import CoverageScheduleReader
from aris.modules.nova.weekly_plan_reader import WeeklyPlanReader
from aris.modules.vega.gpu_hardware_service import VegaGpuHardwareObservations
from aris.modules.vega.scope_service import VegaNodeScopes
from aris.modules.vega.service import VegaService
from aris.modules.vega.usage_archive_service import VegaUsageArchive

PORTAL_ROLES = (
    DefineRole(
        name="admin",
        permissions=(
            "identity.manage",
            "identity.edit_profile",
            "usage.read_own",
            "usage.read_all",
            "usage.read_general",
        ),
    ),
    DefineRole(
        name="member", permissions=("identity.edit_profile", "usage.read_own", "usage.read_general")
    ),
)


class MemberRuntimeConfig(AccessContract):
    namespace: IdentityNamespace
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    origin: str = "http://127.0.0.1:53387"
    user_tokens: tuple[UserTokenBinding, ...] = Field(min_length=1, max_length=128, repr=False)
    confirmation_key: SecretStr = Field(min_length=32, max_length=512, repr=False)
    confirmation_key_id: str = Field(default="members-development", pattern=r"^[A-Za-z0-9_.-]+$")
    confirmation_ttl_seconds: int = Field(default=300, ge=30, le=900, strict=True)
    gateway_limits: GatewayLimits = Field(
        default_factory=lambda: GatewayLimits(
            lock_timeout_ms=50, statement_timeout_ms=100, transaction_timeout_ms=200
        )
    )
    intake_limits: IntakeLimits = Field(
        default_factory=lambda: IntakeLimits(
            lock_timeout_ms=100,
            statement_timeout_ms=500,
            transaction_timeout_ms=1000,
            max_queue_delay_seconds=300,
            max_pending_commands=100,
        )
    )
    worker_limits: CommandWorkerLimits = Field(
        default_factory=lambda: CommandWorkerLimits(
            lock_timeout_ms=500,
            statement_timeout_ms=3000,
            transaction_timeout_ms=5000,
            lease_seconds=15,
            max_attempts=3,
            retry_base_seconds=1,
        )
    )
    poll_seconds: float = Field(default=1, ge=0.1, le=60)
    activity_preview: ActivityPreviewConfig | None = None
    coverage_plan_ids: tuple[UUID, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def fixed_namespace_and_origin(self) -> "MemberRuntimeConfig":
        if len(set(self.coverage_plan_ids)) != len(self.coverage_plan_ids):
            raise ValueError("coverage plan IDs must be unique")
        parts = urlsplit(self.origin)
        if (
            self.namespace.provider != "slack"
            or len(set(self.node_ids)) != len(self.node_ids)
            or parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
            or self.origin != self.origin.rstrip("/")
        ):
            raise ValueError("member portal requires a fixed Slack namespace and exact origin")
        if parts.scheme == "http" and parts.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
            "testserver",
        }:
            raise ValueError("HTTP sessions are restricted to explicit local development")
        if self.activity_preview and any(
            source.node_id not in self.node_ids for source in self.activity_preview.sources
        ):
            raise ValueError("archive source is outside the portal nodes")
        return self


def validate_member_configuration(
    settings: Settings, slack: SlackSettings, config: MemberRuntimeConfig
) -> BoundSubjectTokens:
    if settings.environment not in {"test", "development"}:
        raise ValueError("bound member login is development-only")
    reserved = [
        settings.read_token,
        slack.bot_token,
        slack.signing_secret,
        config.confirmation_key,
        *(value.token for value in settings.agent_credentials.values()),
    ]
    if any(
        hmac.compare_digest(
            config.confirmation_key.get_secret_value().encode(), token.get_secret_value().encode()
        )
        for token in reserved
        if token is not config.confirmation_key
    ):
        raise ValueError("confirmation key must be independent")
    auth = BoundSubjectTokens(config.user_tokens, reserved_tokens=reserved)
    if any(
        (subject.provider, subject.namespace)
        != (config.namespace.provider, config.namespace.namespace)
        for _, subject in auth.bindings
    ):
        raise ValueError("login credentials require the configured workspace")
    if slack.configured and slack.team_id != config.namespace.namespace:
        raise ValueError("Slack workspace differs from the member namespace")
    return auth


@dataclass(frozen=True)
class MemberComponents:
    portal: MemberPortalSurface
    worker: CommandWorker


def build_member_components(
    sessions: sessionmaker[Session],
    settings: Settings,
    slack: SlackSettings,
    config: MemberRuntimeConfig,
    *,
    directory_provider: SlackDirectoryProvider | None = None,
    invitation_sender: InvitationSender | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MemberComponents:
    bindings = validate_member_configuration(settings, slack, config)
    all_permissions = tuple(
        sorted({permission for role in PORTAL_ROLES for permission in role.permissions})
    )
    administration = AccessAdministrationService(
        sessions,
        config.namespace,
        scopes=VegaNodeScopes(sessions),
        permission_catalog=all_permissions,
        registration_roles=(
            RegistrationRoleGrant(role_name="member", scope=AccessScope(type="global", id="*")),
        ),
        clock=clock,
    )
    login = LoginSessionService(
        sessions,
        config.namespace,
        {digest.hex(): subject for digest, subject in bindings.bindings},
        reserved_digests=tuple(
            secret_digest(token.get_secret_value())
            for token in (
                settings.read_token,
                slack.bot_token,
                slack.signing_secret,
                config.confirmation_key,
                *(value.token for value in settings.agent_credentials.values()),
            )
        ),
        clock=clock,
    )
    authentication = SessionBearerAuthenticator(login)
    manager = MemberAdministrationService(administration, login, PORTAL_ROLES)
    definitions = [
        ManageMembersCommand(manager),
        EditOwnProfileCommand(manager),
    ]
    commands = CommandService(
        sessions,
        definitions,
        confirmation_key=config.confirmation_key,
        confirmation_ttl_seconds=config.confirmation_ttl_seconds,
        clock=clock,
    )
    intake = CommandIntakeService(
        commands,
        config.namespace,
        config.intake_limits,
        confirmation_key_id=config.confirmation_key_id,
    )
    surface = UserCommandSurface(
        UserCommandGateway(
            intake, tuple(command.definition.name for command in definitions), config.gateway_limits
        ),
        authentication,
    )
    ceiling = GuardedAccessService(sessions, {"usage.read_all": "identity.manage"})
    history = IndexedUsageHistoryService(sessions, clock=clock)
    history.access = ceiling
    windows = UsageWindowReader(sessions)
    windows.access = ceiling
    plans = WeeklyPlanReader(sessions)
    plans.access = ceiling
    coverage_plans = (
        CoverageScheduleReader(sessions, config.coverage_plan_ids, clock=clock)
        if config.coverage_plan_ids
        else None
    )
    if coverage_plans is not None:
        coverage_plans.access = ceiling
    activity = None
    if config.activity_preview:
        reader = ActivityPreviewService(
            sessions,
            VegaUsageArchive(sessions),
            hardware_source=VegaGpuHardwareObservations(sessions)
            if config.activity_preview.hardware_metrics
            else None,
        )
        reader.access = ceiling
        activity = ActivityPreviewAccess(reader, config.activity_preview)
    usage = UsageHistorySurface(
        history,
        authentication,
        config.node_ids,
        saved_windows=windows,
        weekly_reports=WeeklyReportReader(history),
        weekly_plans=plans,
        coverage_plans=coverage_plans,
        activity=activity,
    )
    roster = None
    if slack.configured or directory_provider is not None:
        roster = IdentityAdminSurface(
            surface,
            AccessDirectoryQueries(administration),
            directory_provider or SlackDirectoryClient(slack),
            config.node_ids,
            IdentityQueryLimits(),
            directory_command="identity.manage_members",
        )
    portal = MemberPortalSurface(
        manager,
        authentication,
        surface,
        usage,
        GeneralUsageService(sessions, clock=clock),
        VegaService(sessions, settings),
        config.node_ids,
        origin=config.origin,
        roster=roster,
        accounts=PasswordAccountService(
            sessions,
            login,
            config.confirmation_key,
            invitation_sender
            or (
                SlackInvitationSender(SlackClient(slack), slack.team_id)
                if slack.configured
                else None
            ),
            clock=clock,
        ),
    )
    return MemberComponents(portal=portal, worker=CommandWorker(intake, config.worker_limits))
