from uuid import UUID

from aris.core.notifications.queries import NotificationOperatorQueries
from aris.core.notifications.query_schemas import (
    NotificationOperatorView,
    UncertainNotificationsPage,
    UncertainNotificationsQuery,
)
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.slack.config import SlackSettings


class NotificationAdminSurface:
    def __init__(self, commands: UserCommandSurface, queries: NotificationOperatorQueries) -> None:
        if not commands.gateway.definition("notification.retry_uncertain").requires_confirmation:
            raise ValueError("notification retries require confirmation")
        self.commands, self.queries = commands, queries

    def validate_workspace(self, settings: SlackSettings) -> None:
        if (
            not settings.configured
            or self.queries.service.namespace.provider != "slack"
            or settings.team_id != self.queries.service.namespace.namespace
        ):
            raise ValueError("notification UI requires the configured Slack workspace")

    def page(
        self, authorization: str | None, request_id: str, query: UncertainNotificationsQuery
    ) -> UncertainNotificationsPage:
        actor = self.commands.actor(authorization, request_id)
        return self.queries.page(actor.subject, query)

    def detail(
        self, authorization: str | None, request_id: str, notification_id: UUID
    ) -> NotificationOperatorView:
        actor = self.commands.actor(authorization, request_id)
        return self.queries.detail(actor.subject, notification_id)
