import hashlib
import json
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessScope
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import (
    ExternalCommandActor,
    ExternalCommandContext,
    ExternalCommandSubmit,
    GatewayLimits,
    UserCommandInput,
)
from aris.core.commands.intake import (
    CommandIntakeService,
    normalized_failure,
    read_authorization_scope,
)
from aris.core.commands.queue_repo import CommandQueueRepository
from aris.core.commands.queue_schemas import (
    CommandIntakeContext,
    DurableActorContext,
    DurableCommandContext,
    IntakeResult,
    WithdrawCommand,
)
from aris.core.commands.schemas import CommandOutcome, CommandRequest, ConfirmCommand
from aris.core.contracts import DomainError


class UserCommandGateway:
    def __init__(
        self, intake: CommandIntakeService, allowed_commands: Sequence[str], limits: GatewayLimits
    ) -> None:
        if (
            not allowed_commands
            or len(allowed_commands) > 32
            or len(set(allowed_commands)) != len(allowed_commands)
        ):
            raise ValueError("external command registrations must be explicit and unique")
        for name in allowed_commands:
            intake.commands._registration(name)
        self.intake, self.limits, self.allowed = intake, limits, frozenset(allowed_commands)

    def _budget(self, session: Session) -> None:
        transaction_budget(
            session,
            self.limits.lock_timeout_ms,
            self.limits.statement_timeout_ms,
            self.limits.transaction_timeout_ms,
        )

    def _actor(self, session: Session, context: ExternalCommandActor) -> DurableActorContext:
        if (context.subject.provider, context.subject.namespace) != (
            self.intake.namespace.provider,
            self.intake.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "User namespace differs", 403)
        principal = self.intake.commands.access.resolve_external_in_transaction(
            session, context.subject
        )
        if principal is None:
            raise DomainError("PERMISSION_DENIED", "User identity is not registered", 403)
        return DurableActorContext(
            actor_id=principal.user_id, subject=context.subject, request_id=context.request_id
        )

    def _denied(self, context: ExternalCommandActor, error: DomainError) -> None:
        try:
            with self.intake.sessions.begin() as session:
                self._budget(session)
                subject_hash = hashlib.sha256(
                    json.dumps(context.subject.model_dump(), sort_keys=True).encode()
                ).hexdigest()
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor="external:" + subject_hash,
                        action="command.gateway.rejected",
                        target="user-command-gateway",
                        request_id=context.request_id,
                        recorded_at=self.intake.commands._now(),
                        changes={"code": normalized_failure(error)[0]},
                    )
                )
        except DBAPIError:
            # No admission started here. Preserve the original key when retrying
            # because a previous request could nevertheless have been accepted.
            raise DomainError(
                "GATEWAY_UNAVAILABLE", "User command context unavailable", 503
            ) from None

    def _resolve(
        self,
        context: ExternalCommandActor,
        *,
        command_id: UUID | None = None,
        request: UserCommandInput | None = None,
    ) -> tuple[DurableActorContext, CommandRequest | None]:
        try:
            with self.intake.sessions.begin() as session:
                self._budget(session)
                actor = self._actor(session, context)
                if request is not None:
                    if request.command not in self.allowed:
                        raise DomainError("PERMISSION_DENIED", "Command is not exposed", 403)
                    _, action = self.intake.commands._registration(request.command)
                    # Normalize and derive target scope from code, never JSON.
                    probe = CommandRequest(
                        command=request.command,
                        scope=AccessScope(type="global", id="*"),
                        parameters=request.parameters,
                    )
                    parameters, _ = self.intake.commands._parameters(action, probe)
                    scope = read_authorization_scope(session, action, parameters)
                    return actor, CommandRequest(
                        command=request.command, scope=scope, parameters=parameters
                    )
                if command_id is not None:
                    record = CommandQueueRepository(session).command(str(command_id))
                    if (
                        record is None
                        or record.actor_id != str(actor.actor_id)
                        or record.command_type not in self.allowed
                    ):
                        raise DomainError("PERMISSION_DENIED", "Command is not available", 403)
                return actor, None
        except DomainError as error:
            self._denied(context, error)
            raise
        except DBAPIError:
            raise DomainError(
                "GATEWAY_UNAVAILABLE", "User command context unavailable", 503
            ) from None
        except Exception:
            failure = DomainError("INTERNAL_ERROR", "User command preparation failed", 500)
            self._denied(context, failure)
            raise failure from None

    def submit(self, context: ExternalCommandSubmit, request: UserCommandInput) -> IntakeResult:
        actor, command = self._resolve(context, request=request)
        assert command is not None
        return self.intake.submit(
            DurableCommandContext(
                **actor.model_dump(),
                receipt_key=context.receipt_key,
                idempotency_key=context.idempotency_key,
            ),
            command,
        )

    def confirm(self, context: ExternalCommandContext, request: ConfirmCommand) -> IntakeResult:
        actor, _ = self._resolve(context, command_id=request.command_id)
        return self.intake.confirm(
            CommandIntakeContext(**actor.model_dump(), receipt_key=context.receipt_key), request
        )

    def withdraw(self, context: ExternalCommandContext, request: WithdrawCommand) -> IntakeResult:
        actor, _ = self._resolve(context, command_id=request.command_id)
        return self.intake.withdraw(
            CommandIntakeContext(**actor.model_dump(), receipt_key=context.receipt_key), request
        )

    def inspect(self, context: ExternalCommandActor, command_id: UUID) -> CommandOutcome:
        actor, _ = self._resolve(context, command_id=command_id)
        return self.intake.inspect(actor, command_id)
