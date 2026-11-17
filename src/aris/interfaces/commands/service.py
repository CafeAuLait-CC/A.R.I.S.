from collections.abc import Sequence
from uuid import UUID

from pydantic import SecretStr

from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import (
    ExternalCommandActor,
    ExternalCommandContext,
    ExternalCommandSubmit,
    UserCommandInput,
)
from aris.core.commands.queue_schemas import IntakeResult, WithdrawCommand
from aris.core.commands.schemas import CommandOutcome, ConfirmCommand
from aris.interfaces.commands.auth import UserCommandAuthenticator


class UserCommandSurface:
    def __init__(
        self, gateway: UserCommandGateway, authentication: UserCommandAuthenticator
    ) -> None:
        self.gateway, self.authentication = gateway, authentication

    def validate_configuration(
        self, environment: str, reserved_tokens: Sequence[SecretStr]
    ) -> None:
        if environment == "production" and self.authentication.development_only:
            raise ValueError("development user credentials cannot enable production commands")
        self.authentication.ensure_distinct_credentials(reserved_tokens)

    def actor(self, authorization: str | None, request_id: str) -> ExternalCommandActor:
        return ExternalCommandActor(
            subject=self.authentication.authenticate(authorization), request_id=request_id
        )

    def submit(
        self, authorization: str | None, request_id: str, key: str, request: UserCommandInput
    ) -> IntakeResult:
        actor = self.actor(authorization, request_id)
        return self.gateway.submit(
            ExternalCommandSubmit(
                **actor.model_dump(), receipt_key="http.submit:" + key, idempotency_key=key
            ),
            request,
        )

    def confirm(
        self, authorization: str | None, request_id: str, key: str, request: ConfirmCommand
    ) -> IntakeResult:
        actor = self.actor(authorization, request_id)
        return self.gateway.confirm(
            ExternalCommandContext(**actor.model_dump(), receipt_key="http.confirm:" + key), request
        )

    def withdraw(
        self, authorization: str | None, request_id: str, key: str, request: WithdrawCommand
    ) -> IntakeResult:
        actor = self.actor(authorization, request_id)
        return self.gateway.withdraw(
            ExternalCommandContext(**actor.model_dump(), receipt_key="http.withdraw:" + key),
            request,
        )

    def inspect(
        self, authorization: str | None, request_id: str, command_id: UUID
    ) -> CommandOutcome:
        return self.gateway.inspect(self.actor(authorization, request_id), command_id)
