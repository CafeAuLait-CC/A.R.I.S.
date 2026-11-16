import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.repo import CommandRepository
from aris.core.commands.schemas import (
    CommandContext,
    CommandDefinition,
    CommandRequest,
    CommandResult,
)
from aris.core.contracts import DomainError

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class DatabaseCommand(ABC):
    """Trusted code registration only; execute must not perform external I/O."""

    definition: CommandDefinition

    @abstractmethod
    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Validate the domain input and return canonical JSON parameters."""

    @abstractmethod
    def execute(
        self, session: Session, principal: UserPrincipal, parameters: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Write only inside the caller-owned transaction; do not commit it."""


def bounded_json(value: dict[str, JsonValue], limit: int, *, result: bool = False) -> bytes:
    try:
        valid = JSON_OBJECT.validate_python(value, strict=True)
        encoded = json.dumps(valid, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > limit:
            raise ValueError("JSON budget exceeded")
        return encoded
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise DomainError(
            "INTERNAL_ERROR" if result else "VALIDATION_ERROR",
            "Invalid command result" if result else "Invalid command parameters",
            500 if result else 422,
        ) from None


class CommandService:
    def __init__(
        self, sessions: sessionmaker[Session], commands: Sequence[DatabaseCommand] = ()
    ) -> None:
        self.sessions = sessions
        self.access = AccessService(sessions)
        self.commands: dict[str, tuple[CommandDefinition, DatabaseCommand]] = {}
        for command in commands:
            definition = command.definition
            if definition.name in self.commands:
                raise ValueError("duplicate command registration")
            self.commands[definition.name] = (definition, command)

    def execute(self, context: CommandContext, request: CommandRequest) -> CommandResult:
        registration = self.commands.get(request.command)
        if registration is None:
            raise DomainError("VALIDATION_ERROR", "Command is not available", 422)
        definition, action = registration
        raw = bounded_json(request.parameters, 16384)
        try:
            parameters = action.normalize(json.loads(raw))
        except (ValidationError, ValueError, TypeError):
            raise DomainError("VALIDATION_ERROR", "Invalid command parameters", 422) from None
        canonical = bounded_json(parameters, 16384)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"command": request.command, "scope": request.scope.model_dump()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
            + canonical
        ).hexdigest()
        with self.sessions.begin() as session:
            principal = self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=context.actor_id,
                    permission=definition.required_permission,
                    scope=request.scope,
                ),
            )
            if definition.requires_confirmation:
                # No confirmation workflow is wired yet. Never accept a flag
                # from parameters as an authorization or confirmation override.
                raise DomainError("CONFIRMATION_REQUIRED", "Command requires confirmation", 409)
            record, created = CommandRepository(session).claim(
                context,
                request,
                parameters,
                hashlib.sha256(context.idempotency_key.encode()).hexdigest(),
                fingerprint,
                datetime.now(UTC),
            )
            if record.fingerprint != fingerprint:
                raise DomainError("CONFLICT", "Idempotency key has different content", 409)
            if not created:
                if record.state != "succeeded" or record.result is None:
                    raise DomainError("CONFLICT", "Command has no committed result", 409)
                return CommandResult(command_id=record.id, duplicate=True, result=record.result)

            def reject_action_commit(_: Session) -> None:
                raise DomainError(
                    "INTERNAL_ERROR", "Command action cannot commit its transaction", 500
                )

            event.listen(session, "before_commit", reject_action_commit)
            try:
                result = action.execute(session, principal, parameters)
            finally:
                event.remove(session, "before_commit", reject_action_commit)
            bounded_json(result, 65536, result=True)
            record.state = "succeeded"
            record.result = result
            record.completed_at = datetime.now(UTC)
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(context.actor_id),
                    action="command.succeeded",
                    target=request.scope.id,
                    request_id=context.request_id,
                    recorded_at=record.completed_at,
                    changes={
                        "command_id": record.id,
                        "command_type": record.command_type,
                        "scope_type": request.scope.type,
                    },
                )
            )
            response = CommandResult(command_id=record.id, duplicate=False, result=result)
        return response
