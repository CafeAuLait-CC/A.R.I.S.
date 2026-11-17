import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.repo import CommandRepository
from aris.core.commands.schemas import (
    ActorContext,
    CancelledCommand,
    CommandContext,
    CommandDefinition,
    CommandExecution,
    CommandOutcome,
    CommandRequest,
    CommandResult,
    ConfirmCommand,
    ExpiredCommand,
    FailedCommand,
    PendingConfirmation,
    QueuedCommand,
)
from aris.core.contracts import DomainError
from aris.core.time import utc

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class DatabaseCommand(ABC):
    """Trusted code registration only; execute must not perform external I/O."""

    definition: CommandDefinition

    @abstractmethod
    def normalize(self, parameters: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Validate the domain input and return canonical JSON parameters."""

    @abstractmethod
    def authorization_scope(
        self, session: Session, parameters: dict[str, JsonValue]
    ) -> AccessScope:
        """Read-only derivation from the actual target, never from client scope."""

    @abstractmethod
    def execute(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
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
        self,
        sessions: sessionmaker[Session],
        commands: Sequence[DatabaseCommand] = (),
        *,
        confirmation_key: SecretStr | None = None,
        confirmation_ttl_seconds: int = 300,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self.access = AccessService(sessions)
        if confirmation_key is not None and len(confirmation_key.get_secret_value()) < 32:
            raise ValueError("confirmation key is too short")
        if not 30 <= confirmation_ttl_seconds <= 900:
            raise ValueError("confirmation TTL must be 30–900 seconds")
        self.confirmation_key = confirmation_key
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.commands: dict[str, tuple[CommandDefinition, DatabaseCommand]] = {}
        for command in commands:
            definition = command.definition
            if definition.name in self.commands:
                raise ValueError("duplicate command registration")
            self.commands[definition.name] = (definition, command)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("command clock requires a timezone")
        return utc(now)

    def _registration(self, name: str) -> tuple[CommandDefinition, DatabaseCommand]:
        registration = self.commands.get(name)
        if registration is None:
            raise DomainError("VALIDATION_ERROR", "Command is not available", 422)
        return registration

    @staticmethod
    def _parameters(
        action: DatabaseCommand, request: CommandRequest
    ) -> tuple[dict[str, JsonValue], str]:
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
        return parameters, fingerprint

    def _proof(self, record: CommandRecord) -> str:
        if self.confirmation_key is None:
            raise DomainError("CONFIRMATION_REQUIRED", "Command requires confirmation", 409)
        if record.confirmation_expires_at is None:
            raise DomainError("CONFLICT", "Command did not request confirmation", 409)
        fields = {
            "command_id": record.id,
            "actor_id": record.actor_id,
            "fingerprint": record.fingerprint,
            "expires_at": utc(record.confirmation_expires_at).isoformat(),
        }
        if record.execution_not_after is not None:
            fields["execution_not_after"] = utc(record.execution_not_after).isoformat()
        message = json.dumps(
            fields,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(
            self.confirmation_key.get_secret_value().encode(),
            b"aris.command.confirm\x00" + message,
            hashlib.sha256,
        ).hexdigest()

    def _audit(
        self, session: Session, context: ActorContext, record: CommandRecord, action: str
    ) -> None:
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=str(context.actor_id),
                action=action,
                target=record.scope_id,
                request_id=context.request_id,
                recorded_at=self._now(),
                changes={
                    "command_id": record.id,
                    "command_type": record.command_type,
                    "scope_type": record.scope_type,
                },
            )
        )

    def _existing(
        self, session: Session, context: ActorContext, record: CommandRecord
    ) -> CommandOutcome:
        if record.state == "succeeded" and record.result is not None:
            bounded_json(record.result, 65536, result=True)
            return CommandResult(command_id=record.id, duplicate=True, result=record.result)
        if record.state == "expired":
            return ExpiredCommand(command_id=record.id)
        if record.state == "cancelled":
            return CancelledCommand(command_id=record.id)
        if record.state == "failed":
            if record.failure_code is None:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Command failure metadata is invalid", 503
                )
            try:
                return FailedCommand(command_id=record.id, code=record.failure_code)
            except ValidationError:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Command failure metadata is invalid", 503
                ) from None
        if record.state == "queued" and record.execution_deadline is not None:
            deadline = utc(record.execution_deadline)
            return QueuedCommand(
                command_id=record.id, execute_by=deadline, deadline_elapsed=self._now() >= deadline
            )
        if record.state == "awaiting_confirmation" and record.confirmation_expires_at is not None:
            if self._now() >= utc(record.confirmation_expires_at):
                record.state = "expired"
                self._audit(session, context, record, "command.expired")
                return ExpiredCommand(command_id=record.id)
            return PendingConfirmation(
                command_id=record.id,
                expires_at=utc(record.confirmation_expires_at),
                proof=SecretStr(self._proof(record)),
                execution_not_after=utc(record.execution_not_after)
                if record.execution_not_after is not None
                else None,
            )
        raise DomainError("CONFLICT", "Command has no committed result", 409)

    def _complete(
        self,
        session: Session,
        context: ActorContext,
        record: CommandRecord,
        action: DatabaseCommand,
        principal: UserPrincipal,
        parameters: dict[str, JsonValue],
        execution_deadline: datetime | None = None,
    ) -> CommandResult:
        if execution_deadline is not None and self._now() >= execution_deadline:
            raise DomainError("EXECUTION_EXPIRED", "Command execution deadline elapsed", 409)

        def reject_action_commit(active: Session) -> None:
            if not active.in_nested_transaction():
                raise DomainError(
                    "INTERNAL_ERROR", "Command action cannot commit its transaction", 500
                )

        event.listen(session, "before_commit", reject_action_commit)
        try:
            result = action.execute(
                session,
                CommandExecution(
                    command_id=record.id,
                    actor_id=context.actor_id,
                    request_id=context.request_id,
                    scope=AccessScope(type=record.scope_type, id=record.scope_id),
                    execution_deadline=execution_deadline,
                ),
                principal,
                parameters,
            )
        finally:
            event.remove(session, "before_commit", reject_action_commit)
        bounded_json(result, 65536, result=True)
        if execution_deadline is not None and self._now() >= execution_deadline:
            raise DomainError("EXECUTION_EXPIRED", "Command execution deadline elapsed", 409)
        record.state, record.result, record.completed_at = "succeeded", result, self._now()
        self._audit(session, context, record, "command.succeeded")
        return CommandResult(command_id=record.id, duplicate=False, result=result)

    def execute(self, context: CommandContext, request: CommandRequest) -> CommandOutcome:
        definition, action = self._registration(request.command)
        parameters, fingerprint = self._parameters(action, request)
        with self.sessions.begin() as session:
            if action.authorization_scope(session, parameters) != request.scope:
                raise DomainError("PERMISSION_DENIED", "Command target scope differs", 403)
            principal = self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=context.actor_id,
                    permission=definition.required_permission,
                    scope=request.scope,
                ),
            )
            if definition.requires_confirmation and self.confirmation_key is None:
                raise DomainError("CONFIRMATION_REQUIRED", "Command requires confirmation", 409)
            record, created = CommandRepository(session).claim(
                context,
                request,
                parameters,
                hashlib.sha256(context.idempotency_key.encode()).hexdigest(),
                fingerprint,
                self._now(),
            )
            if record.fingerprint != fingerprint:
                raise DomainError("CONFLICT", "Idempotency key has different content", 409)
            if record.execution_not_after is not None:
                raise DomainError("CONFLICT", "Command requires durable intake", 409)
            if not created:
                return self._existing(session, context, record)
            if definition.requires_confirmation:
                record.state = "awaiting_confirmation"
                record.confirmation_expires_at = self._now() + timedelta(
                    seconds=self.confirmation_ttl_seconds
                )
                self._audit(session, context, record, "command.awaiting_confirmation")
                return self._existing(session, context, record)
            return self._complete(session, context, record, action, principal, parameters)

    def confirm(
        self, context: ActorContext, confirmation: ConfirmCommand
    ) -> CommandResult | ExpiredCommand:
        with self.sessions.begin() as session:
            record = CommandRepository(session).lock_owned(
                str(confirmation.command_id), str(context.actor_id)
            )
            if record is None:
                raise DomainError("PERMISSION_DENIED", "Confirmation is invalid", 403)
            if record.execution_not_after is not None:
                raise DomainError("CONFLICT", "Command requires durable confirmation", 409)
            definition, action = self._registration(record.command_type)
            request = CommandRequest(
                command=record.command_type,
                scope={"type": record.scope_type, "id": record.scope_id},
                parameters=record.parameters,
            )
            parameters, fingerprint = self._parameters(action, request)
            if action.authorization_scope(session, parameters) != request.scope:
                raise DomainError("PERMISSION_DENIED", "Command target scope differs", 403)
            principal = self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=context.actor_id,
                    permission=definition.required_permission,
                    scope=request.scope,
                ),
            )
            if not hmac.compare_digest(
                self._proof(record).encode(), confirmation.proof.get_secret_value().encode()
            ):
                raise DomainError("PERMISSION_DENIED", "Confirmation is invalid", 403)
            if record.state in {"succeeded", "expired"}:
                outcome = self._existing(session, context, record)
                assert isinstance(outcome, (CommandResult, ExpiredCommand))
                return outcome
            outcome = self._existing(session, context, record)
            if isinstance(outcome, ExpiredCommand):
                return outcome
            if not isinstance(outcome, PendingConfirmation):
                raise DomainError("CONFLICT", "Command is not awaiting confirmation", 409)
            if record.fingerprint != fingerprint:
                raise DomainError("CONFLICT", "Command content no longer matches", 409)
            self._audit(session, context, record, "command.confirmed")
            return self._complete(session, context, record, action, principal, parameters)
