from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import Literal

from pydantic import Field, JsonValue, SecretStr, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import GatewayLimits
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.queue_schemas import CommandWorkerLimits, IntakeLimits
from aris.core.commands.service import CommandService, DatabaseCommand
from aris.core.commands.worker import CommandWorker
from aris.core.config import Settings
from aris.core.notifications.dispatch import NotificationDispatcher, NotificationTransport
from aris.core.notifications.schemas import NotificationLimits
from aris.core.notifications.service import NotificationService
from aris.core.relay.schemas import RelayLimits
from aris.core.relay.service import EventRelay
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.reservations.service import PersonalReservationQueries, UserReservationSurface
from aris.interfaces.slack.client import HomePublisher, ModalPublisher, SlackClient
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.dispatch import SlackDispatcher
from aris.interfaces.slack.home_reservations import HomeReservationSource
from aris.interfaces.slack.notifications import SlackNotificationTransport
from aris.interfaces.slack.reservation_drafts import (
    ReservationReviewPresenter,
    SlackReservationDrafts,
)
from aris.interfaces.slack.reservations import SlackReservationQueries
from aris.interfaces.slack.user_commands import SlackCommandAdapter
from aris.modules.nova.administration import ReservationAdministrationService
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import BasicReservationRules, MaintenanceCoverage
from aris.modules.nova.commands import (
    CancelAnyReservationCommand,
    CancelReservationCommand,
    CreateReservationCommand,
    ExtendReservationCommand,
    ReleaseAnyReservationCommand,
    ReleaseReservationCommand,
)
from aris.modules.nova.extension import ReservationExtensionService
from aris.modules.nova.notifications import (
    ReservationNoticeTemplate,
    ReservationNotificationConsumer,
)
from aris.modules.nova.occupancy import ReservationOccupancyService
from aris.modules.nova.reservation_service import ReservationService
from aris.modules.vega.service import VegaService
from aris.modules.vega.target_service import VegaTargetService

CommandName = Literal[
    "reservation.create",
    "reservation.cancel",
    "reservation.release",
    "reservation.extend",
    "reservation.cancel_any",
    "reservation.release_any",
]
WorkerMode = Literal["commands", "events", "notifications", "home", "all"]


class M2RuntimeConfig(AccessContract):
    namespace: IdentityNamespace
    commands: tuple[CommandName, ...] = Field(min_length=1, max_length=6)
    confirmation_key: SecretStr = Field(min_length=32, max_length=512, repr=False)
    confirmation_key_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    confirmation_ttl_seconds: int = Field(ge=30, le=900, strict=True)
    gateway_limits: GatewayLimits
    intake_limits: IntakeLimits
    command_worker_limits: CommandWorkerLimits
    relay_limits: RelayLimits
    notification_limits: NotificationLimits
    rules: BasicReservationRules | None = None
    maintenance: tuple[MaintenanceCoverage, ...] = ()
    user_tokens: tuple[UserTokenBinding, ...] = Field(default=(), max_length=128, repr=False)
    slack_callbacks: bool = Field(default=False, strict=True)
    notification_delivery: bool = Field(default=False, strict=True)
    reservation_home: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def explicit_authorities(self) -> "M2RuntimeConfig":
        if self.namespace.provider != "slack" or len(set(self.commands)) != len(self.commands):
            raise ValueError("M2 requires an explicit Slack namespace and unique commands")
        if any(name in self.commands for name in ("reservation.create", "reservation.extend")):
            if self.rules is None or not self.maintenance:
                raise ValueError(
                    "create/extend require explicit basic rules and maintenance coverage"
                )
        if len({item.gpu_uuid for item in self.maintenance}) != len(self.maintenance):
            raise ValueError("duplicate maintenance coverage")
        for binding in self.user_tokens:
            if (binding.subject.provider, binding.subject.namespace) != (
                self.namespace.provider,
                self.namespace.namespace,
            ):
                raise ValueError("user credential namespace differs")
        return self


def load_m2_config(path: Path) -> M2RuntimeConfig:
    metadata = path.lstat()
    if not S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 1048576:
        raise ValueError("M2 configuration requires a private regular file of at most 1 MiB")
    return M2RuntimeConfig.model_validate_json(path.read_bytes())


def reserved_credentials(
    settings: Settings, slack: SlackSettings, config: M2RuntimeConfig
) -> list[SecretStr]:
    return [
        settings.read_token,
        *(item.token for item in settings.agent_credentials.values()),
        slack.bot_token,
        slack.signing_secret,
        config.confirmation_key,
    ]


def validate_m2_configuration(
    settings: Settings, slack: SlackSettings, config: M2RuntimeConfig
) -> None:
    key = config.confirmation_key.get_secret_value()
    other_keys = [
        settings.read_token,
        *(item.token for item in settings.agent_credentials.values()),
        slack.bot_token,
        slack.signing_secret,
    ]
    if any(item.get_secret_value() and item.get_secret_value() == key for item in other_keys):
        raise ValueError("confirmation key must be independent of interface credentials")
    if config.user_tokens:
        auth = BoundSubjectTokens(
            config.user_tokens, reserved_tokens=reserved_credentials(settings, slack, config)
        )
        if settings.environment == "production" and auth.development_only:
            raise ValueError("development user credentials cannot enable production commands")
    if (config.slack_callbacks or config.reservation_home) and (
        not slack.configured or slack.team_id != config.namespace.namespace
    ):
        raise ValueError("Slack callbacks require the configured identity workspace")
    if config.notification_delivery and (
        not slack.bot_token.get_secret_value() or slack.team_id != config.namespace.namespace
    ):
        raise ValueError("notification delivery requires the configured bot and workspace")


@dataclass(frozen=True)
class M2Components:
    intake: CommandIntakeService
    user_api: UserCommandSurface | None
    user_reservations: UserReservationSurface | None
    slack_callbacks: SlackCommandAdapter | None
    slack_reservations: SlackReservationQueries | None
    slack_drafts: SlackReservationDrafts | None
    commands: CommandWorker
    events: EventRelay
    notifications: NotificationDispatcher | None
    home_reservations: HomeReservationSource | None
    home: SlackDispatcher | None

    def run_once(self, mode: WorkerMode) -> dict[str, JsonValue]:
        if mode not in {"commands", "events", "notifications", "home", "all"}:
            raise ValueError("unsupported M2 worker mode")
        if mode == "notifications" and self.notifications is None:
            raise ValueError("notification delivery is disabled")
        if mode == "home" and self.home is None:
            raise ValueError("reservation Home is disabled")
        report: dict[str, JsonValue] = {"mode": "m2-" + mode}
        if mode in {"commands", "all"}:
            result = self.commands.run_once()
            report["commands"] = result.model_dump(mode="json") if result is not None else None
        if mode in {"events", "all"}:
            report["publication"] = self.events.publish_once().model_dump(mode="json")
            delivery = self.events.run_once()
            report["event_delivery"] = (
                delivery.model_dump(mode="json") if delivery is not None else None
            )
        if mode in {"notifications", "all"}:
            report["notification_delivery_enabled"] = self.notifications is not None
            if self.notifications is not None:
                notification = self.notifications.run_once()
                report["notification"] = (
                    notification.model_dump(mode="json") if notification is not None else None
                )
        if mode in {"home", "all"} and self.home is not None:
            report["home"] = self.home.run_once().model_dump(mode="json")
        return report


def build_m2_components(
    sessions: sessionmaker[Session],
    settings: Settings,
    slack: SlackSettings,
    config: M2RuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
    notification_transport: NotificationTransport | None = None,
    modal_publisher: ModalPublisher | None = None,
    home_publisher: HomePublisher | None = None,
) -> M2Components:
    validate_m2_configuration(settings, slack, config)
    clock = clock or (lambda: datetime.now(UTC))
    targets = VegaTargetService(sessions, settings, clock=clock)
    available = AvailabilityService(
        sessions, targets, rules=config.rules, maintenance=config.maintenance, clock=clock
    )
    reservations = ReservationService(sessions, available)
    extensions = ReservationExtensionService(sessions, available, clock=clock)
    administration = ReservationAdministrationService(sessions, clock=clock)
    available_commands: dict[str, DatabaseCommand] = {
        "reservation.create": CreateReservationCommand(reservations),
        "reservation.cancel": CancelReservationCommand(reservations),
        "reservation.release": ReleaseReservationCommand(reservations),
        "reservation.extend": ExtendReservationCommand(extensions),
        "reservation.cancel_any": CancelAnyReservationCommand(administration),
        "reservation.release_any": ReleaseAnyReservationCommand(administration),
    }
    commands = CommandService(
        sessions,
        [available_commands[name] for name in config.commands],
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
    gateway = UserCommandGateway(intake, config.commands, config.gateway_limits)
    personal_queries = PersonalReservationQueries(
        sessions, reservations, config.namespace, config.gateway_limits
    )
    user_api = (
        UserCommandSurface(
            gateway,
            BoundSubjectTokens(
                config.user_tokens, reserved_tokens=reserved_credentials(settings, slack, config)
            ),
        )
        if config.user_tokens
        else None
    )
    publisher = modal_publisher or SlackClient(slack)
    review_presenter = ReservationReviewPresenter(sessions, targets, config.gateway_limits)
    callbacks = (
        SlackCommandAdapter(gateway, publisher, review_presenter)
        if config.slack_callbacks
        else None
    )
    notifications = NotificationService(
        sessions, config.namespace, [ReservationNoticeTemplate()], clock=clock
    )
    relay = EventRelay(
        sessions,
        [ReservationNotificationConsumer(notifications, targets)],
        config.relay_limits,
        clock=clock,
    )
    dispatcher = (
        NotificationDispatcher(
            notifications,
            config.notification_limits,
            notification_transport or SlackNotificationTransport(slack),
        )
        if config.notification_delivery
        else None
    )
    home_reservations = (
        HomeReservationSource(
            sessions,
            config.namespace,
            targets,
            ReservationOccupancyService(sessions, clock=clock),
            config.gateway_limits,
            clock,
        )
        if config.reservation_home
        else None
    )
    return M2Components(
        intake=intake,
        user_api=user_api,
        user_reservations=(
            UserReservationSurface(
                personal_queries,
                user_api.authentication,
            )
            if user_api is not None
            else None
        ),
        slack_callbacks=callbacks,
        slack_reservations=(
            SlackReservationQueries(personal_queries) if config.slack_callbacks else None
        ),
        slack_drafts=(
            SlackReservationDrafts(gateway, publisher, review_presenter)
            if config.slack_callbacks and "reservation.create" in config.commands
            else None
        ),
        commands=CommandWorker(intake, config.command_worker_limits),
        events=relay,
        notifications=dispatcher,
        home_reservations=home_reservations,
        home=(
            SlackDispatcher(
                sessions,
                slack,
                VegaService(sessions, settings),
                home_publisher or SlackClient(slack),
                reservations=home_reservations,
                clock=clock,
            )
            if home_reservations is not None
            else None
        ),
    )
