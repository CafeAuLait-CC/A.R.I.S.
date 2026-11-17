from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.commands.schemas import CommandDefinition, CommandExecution
from aris.core.commands.service import DatabaseCommand
from aris.core.contracts import DomainError
from aris.core.notifications.administration import (
    MANAGE_NOTIFICATIONS,
    NotificationAdminContext,
    NotificationAdministrationService,
    RetryUncertainNotification,
)


class RetryUncertainNotificationCommand(DatabaseCommand):
    definition = CommandDefinition(
        name="notification.retry_uncertain", required_permission=MANAGE_NOTIFICATIONS
    )

    def __init__(self, administration: NotificationAdministrationService) -> None:
        self.administration = administration

    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return RetryUncertainNotification.model_validate(parameters).model_dump(mode="json")

    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        return AccessScope(type="global", id="*")

    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if (
            execution.scope != AccessScope(type="global", id="*")
            or principal.user_id != execution.actor_id
        ):
            raise DomainError("PERMISSION_DENIED", "Notification administrator scope differs", 403)
        return self.administration.retry_in_transaction(
            session,
            NotificationAdminContext(
                actor_id=execution.actor_id,
                operation_id=execution.command_id,
                request_id=execution.request_id,
            ),
            RetryUncertainNotification.model_validate(parameters),
        ).model_dump(mode="json")
