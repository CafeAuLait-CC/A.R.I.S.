import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.models import (
    CommandAdmission,
    CommandIntakeReceipt,
    CommandRecord,
)
from aris.core.commands.queue_repo import CommandQueueRepository
from aris.core.commands.queue_schemas import (
    CommandIntakeContext,
    DurableActorContext,
    DurableCommandContext,
    IntakeLimits,
    IntakeResult,
    WithdrawCommand,
)
from aris.core.commands.repo import CommandRepository
from aris.core.commands.schemas import (
    CancelledCommand,
    CommandContext,
    CommandDefinition,
    CommandOutcome,
    CommandRequest,
    CommandReview,
    ConfirmCommand,
    ExpiredCommand,
)
from aris.core.commands.service import CommandService, DatabaseCommand
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.time import utc


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()
    ).hexdigest()


def normalized_failure(error: DomainError) -> tuple[str, int, bool]:
    code = error.code if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", error.code) else "INTERNAL_ERROR"
    status = error.status if 400 <= error.status <= 599 else 500
    return (
        code,
        status,
        code in {"QUEUE_BUSY", "QUEUE_FULL", "RETRYABLE_DATABASE_ERROR", "INTAKE_BUSY"},
    )


def read_authorization_scope(
    session: Session, action: DatabaseCommand, parameters: dict[str, JsonValue]
) -> AccessScope:
    # This guards accidental writes/commits in trusted scope providers, not
    # hostile SQL functions or external I/O by arbitrary Python plugins.
    with session.begin_nested() as nested:
        connection = session.connection()

        def select_only(
            conn: Connection, cursor: Any, statement: str, values: Any, context: Any, many: bool
        ) -> None:
            if not statement.lstrip().upper().startswith("SELECT"):
                raise DomainError("INTERNAL_ERROR", "Authorization provider attempted a write", 500)

        def no_commit(active: Session) -> None:
            raise DomainError("INTERNAL_ERROR", "Authorization provider cannot commit", 500)

        event.listen(connection, "before_cursor_execute", select_only)
        event.listen(session, "before_commit", no_commit)
        try:
            with session.no_autoflush:
                scope = action.authorization_scope(session, parameters)
            if session.new or session.dirty or session.deleted:
                raise DomainError("INTERNAL_ERROR", "Authorization provider changed data", 500)
            return scope
        finally:
            event.remove(connection, "before_cursor_execute", select_only)
            event.remove(session, "before_commit", no_commit)
            if nested.is_active:
                nested.rollback()


class CommandIntakeService:
    def __init__(
        self,
        commands: CommandService,
        namespace: IdentityNamespace,
        limits: IntakeLimits,
        *,
        confirmation_key_id: str,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", confirmation_key_id):
            raise ValueError("confirmation key ID must be a nonsecret identifier")
        self.commands, self.sessions, self.namespace, self.limits = (
            commands,
            commands.sessions,
            namespace,
            limits,
        )
        self.confirmation_key_id = confirmation_key_id

    def _budget(self, session: Session) -> None:
        transaction_budget(
            session,
            self.limits.lock_timeout_ms,
            self.limits.statement_timeout_ms,
            self.limits.transaction_timeout_ms,
        )

    def identity(self, session: Session, context: DurableActorContext) -> UserPrincipal:
        if (context.subject.provider, context.subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "Identity namespace differs", 403)
        principal = self.commands.access.resolve_external_in_transaction(session, context.subject)
        if principal is None or principal.user_id != context.actor_id:
            raise DomainError("PERMISSION_DENIED", "Current external identity differs", 403)
        return principal

    def authorize(
        self, session: Session, context: DurableActorContext, request: CommandRequest
    ) -> tuple[CommandDefinition, DatabaseCommand, UserPrincipal]:
        self.identity(session, context)
        definition, action = self.commands._registration(request.command)
        if read_authorization_scope(session, action, request.parameters) != request.scope:
            raise DomainError("PERMISSION_DENIED", "Command target scope differs", 403)
        principal = self.commands.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=context.actor_id,
                permission=definition.required_permission,
                scope=request.scope,
            ),
        )
        return definition, action, principal

    def key_tag(self) -> str:
        key = self.commands.confirmation_key
        if key is None:
            raise DomainError(
                "CONFIRMATION_REQUIRED", "Command confirmation is not configured", 409
            )
        return hmac.new(
            key.get_secret_value().encode(),
            b"aris.command.key-tag\x00" + self.confirmation_key_id.encode(),
            hashlib.sha256,
        ).hexdigest()

    def audit(
        self,
        session: Session,
        context: DurableActorContext,
        action: str,
        target: str,
        **changes: JsonValue,
    ) -> None:
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=str(context.actor_id),
                action=action,
                target=target,
                request_id=context.request_id,
                recorded_at=self.commands._now(),
                changes=changes,
            )
        )

    def _admission(
        self, session: Session, context: DurableActorContext, record: CommandRecord
    ) -> CommandAdmission:
        if record.actor_id != str(context.actor_id) or record.execution_not_after is None:
            raise DomainError("PERMISSION_DENIED", "Durable command is not available", 403)
        admission = CommandQueueRepository(session).admission(record.id)
        if admission is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Command admission is missing", 503)
        if (admission.provider, admission.namespace, admission.external_id) != (
            context.subject.provider,
            context.subject.namespace,
            context.subject.external_id,
        ):
            raise DomainError("PERMISSION_DENIED", "Command subject differs", 403)
        self.identity(session, context)
        return admission

    def current_request(
        self, session: Session, context: DurableActorContext, record: CommandRecord
    ) -> tuple[CommandAdmission, CommandDefinition, DatabaseCommand, UserPrincipal, CommandRequest]:
        admission = self._admission(session, context, record)
        request = CommandRequest(
            command=record.command_type,
            scope={"type": record.scope_type, "id": record.scope_id},
            parameters=record.parameters,
        )
        definition, action, principal = self.authorize(session, context, request)
        parameters, fingerprint = self.commands._parameters(action, request)
        if (
            fingerprint != record.fingerprint
            or digest(definition.model_dump()) != admission.definition_fingerprint
        ):
            raise DomainError(
                "COMMAND_DEFINITION_CHANGED", "Command definition or intent changed", 409
            )
        return (
            admission,
            definition,
            action,
            principal,
            request.model_copy(update={"parameters": parameters}),
        )

    def outcome(
        self, session: Session, context: DurableActorContext, record: CommandRecord
    ) -> CommandOutcome:
        admission, _, _, _, _ = self.current_request(session, context, record)
        return self._validated_outcome(session, context, record, admission)

    def _validated_outcome(
        self,
        session: Session,
        context: DurableActorContext,
        record: CommandRecord,
        admission: CommandAdmission,
    ) -> CommandOutcome:
        if record.state == "awaiting_confirmation":
            if (
                admission.confirmation_key_id != self.confirmation_key_id
                or admission.confirmation_key_tag is None
                or not hmac.compare_digest(
                    admission.confirmation_key_tag.encode(), self.key_tag().encode()
                )
            ):
                raise DomainError(
                    "CONFIRMATION_KEY_CHANGED", "Start a new confirmation intent", 409
                )
        return self.commands._existing(session, context, record)

    def _run(
        self,
        context: CommandIntakeContext,
        kind: Literal["submit", "confirm", "withdraw"],
        fingerprint: str,
        operation: Callable[[Session], CommandOutcome],
    ) -> IntakeResult:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                repo = CommandQueueRepository(session)
                receipt, created = repo.claim_receipt(
                    context,
                    kind,
                    fingerprint,
                    digest(context.subject.model_dump()),
                    self.commands._now(),
                )
                if (
                    receipt.fingerprint != fingerprint
                    or receipt.kind != kind
                    or receipt.subject_fingerprint != digest(context.subject.model_dump())
                ):
                    self.audit(
                        session, context, "command.intake.conflict", receipt.id, code="CONFLICT"
                    )
                    return IntakeResult(
                        receipt_id=receipt.id,
                        disposition="rejected",
                        duplicate=True,
                        error_code="CONFLICT",
                        error_status=409,
                    )
                replay = not created
                if replay and receipt.state == "rejected" and not receipt.retryable:
                    return IntakeResult(
                        receipt_id=receipt.id,
                        disposition="rejected",
                        duplicate=True,
                        error_code=receipt.error_code,
                        error_status=receipt.error_status,
                    )
                if replay and receipt.state == "rejected":
                    receipt.attempts += 1
                    receipt.state, receipt.error_code, receipt.error_status, receipt.retryable = (
                        "receiving",
                        None,
                        None,
                        False,
                    )
                preserve = receipt.state == "accepted"
                try:
                    with transaction_savepoint(session):
                        self.identity(session, context)
                        if preserve:
                            record = repo.command(receipt.command_id or "", lock=True)
                            if record is None:
                                raise DomainError(
                                    "DEPENDENCY_UNAVAILABLE", "Receipt command is missing", 503
                                )
                            if kind == "withdraw":
                                self._admission(session, context, record)
                                outcome = self.commands._existing(session, context, record)
                            else:
                                outcome = self.outcome(session, context, record)
                        else:
                            outcome = operation(session)
                except DBAPIError as error:
                    code = database_failure(error)
                    failure = DomainError(
                        code,
                        "Database admission operation failed",
                        503 if code == "RETRYABLE_DATABASE_ERROR" else 500,
                    )
                    return self._reject(session, context, receipt, failure, replay, preserve)
                except DomainError as error:
                    return self._reject(session, context, receipt, error, replay, preserve)
                except Exception:
                    return self._reject(
                        session,
                        context,
                        receipt,
                        DomainError("INTERNAL_ERROR", "Command admission failed", 500),
                        replay,
                        preserve,
                    )
                receipt.state, receipt.command_id = "accepted", str(outcome.command_id)
                receipt.error_code, receipt.error_status, receipt.retryable = None, None, False
                receipt.updated_at = self.commands._now()
                if not preserve:
                    self.audit(
                        session,
                        context,
                        "command.intake.accepted",
                        receipt.id,
                        command_id=receipt.command_id,
                        kind=kind,
                        attempt=receipt.attempts,
                    )
                return IntakeResult(
                    receipt_id=receipt.id, disposition="accepted", duplicate=replay, outcome=outcome
                )
        except DBAPIError:
            raise DomainError(
                "INTAKE_UNAVAILABLE", "Admission outcome unavailable; retry the same keys", 503
            ) from None

    def _reject(
        self,
        session: Session,
        context: CommandIntakeContext,
        receipt: CommandIntakeReceipt,
        error: DomainError,
        duplicate: bool,
        preserve: bool,
    ) -> IntakeResult:
        code, status, retryable = normalized_failure(error)
        if not preserve:
            receipt.state, receipt.error_code, receipt.error_status, receipt.retryable = (
                "rejected",
                code,
                status,
                retryable,
            )
            receipt.command_id = None
            receipt.updated_at = self.commands._now()
        self.audit(
            session,
            context,
            "command.intake.rejected",
            receipt.id,
            code=code,
            kind=receipt.kind,
            attempt=receipt.attempts,
            preserved_original=preserve,
        )
        return IntakeResult(
            receipt_id=receipt.id,
            disposition="rejected",
            duplicate=duplicate,
            error_code=code,
            error_status=status,
            retryable=retryable,
        )

    def submit(self, context: DurableCommandContext, request: CommandRequest) -> IntakeResult:
        prepared = None
        failure = None
        content: dict[str, Any]
        try:
            definition, action = self.commands._registration(request.command)
            parameters, fingerprint = self.commands._parameters(action, request)
            prepared = (definition, parameters, fingerprint)
            content = {"definition": definition.model_dump(), "fingerprint": fingerprint}
        except DomainError as error:
            failure = error
            content = {"request": request.model_dump(mode="json"), "invalid": True}
        except Exception:
            failure = DomainError("INTERNAL_ERROR", "Command normalization failed", 500)
            content = {"request": request.model_dump(mode="json"), "invalid": True}
        key_hash = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        request_fingerprint = digest({"kind": "submit", "key": key_hash, "content": content})

        def accept(session: Session) -> CommandOutcome:
            if failure is not None:
                raise failure
            if prepared is None:
                raise DomainError("INTERNAL_ERROR", "Prepared command is missing", 500)
            definition, parameters, fingerprint = prepared
            normalized = CommandRequest(
                command=request.command, scope=request.scope, parameters=parameters
            )
            self.authorize(session, context, normalized)
            repo = CommandQueueRepository(session)
            existing = repo.by_key(str(context.actor_id), context.idempotency_key)
            if existing is None:
                if not repo.capacity_guard():
                    raise DomainError("QUEUE_BUSY", "Command admission is busy", 503)
                if repo.pending_count(self.commands._now()) >= self.limits.max_pending_commands:
                    raise DomainError("QUEUE_FULL", "Command admission is full", 503)
            record, created = CommandRepository(session).claim(
                CommandContext(
                    actor_id=context.actor_id,
                    request_id=context.request_id,
                    idempotency_key=context.idempotency_key,
                ),
                normalized,
                parameters,
                key_hash,
                fingerprint,
                self.commands._now(),
            )
            if record.fingerprint != fingerprint:
                raise DomainError("CONFLICT", "Idempotency key has different content", 409)
            if not created:
                return self.outcome(session, context, record)
            now = self.commands._now()
            delay = self.limits.max_queue_delay_seconds
            admission = CommandAdmission(
                command_id=record.id,
                provider=context.subject.provider,
                namespace=context.subject.namespace,
                external_id=context.subject.external_id,
                definition_fingerprint=digest(definition.model_dump()),
                max_queue_delay_seconds=delay,
                confirmation_required=definition.requires_confirmation,
                confirmation_key_id=self.confirmation_key_id,
                confirmation_key_tag=self.key_tag() if definition.requires_confirmation else None,
                verified_at=None,
                execution_deadline=None,
                verified_fingerprint=None,
                verification_kind=None,
            )
            repo.add_admission(admission)
            if definition.requires_confirmation:
                record.state = "awaiting_confirmation"
                record.confirmation_expires_at = now + timedelta(
                    seconds=self.commands.confirmation_ttl_seconds
                )
                record.execution_not_after = record.confirmation_expires_at + timedelta(
                    seconds=delay
                )
                self.commands._audit(session, context, record, "command.awaiting_confirmation")
            else:
                record.execution_not_after = now + timedelta(seconds=delay)
                self._queue(session, context, record, admission, "not_required")
            session.flush()
            return self.outcome(session, context, record)

        return self._run(context, "submit", request_fingerprint, accept)

    def _queue(
        self,
        session: Session,
        context: DurableActorContext,
        record: CommandRecord,
        admission: CommandAdmission,
        kind: Literal["proof", "not_required"],
    ) -> None:
        now = self.commands._now()
        if record.execution_not_after is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Execution bound is missing", 503)
        deadline = min(
            utc(record.execution_not_after),
            now
            + timedelta(
                seconds=min(admission.max_queue_delay_seconds, self.limits.max_queue_delay_seconds)
            ),
        )
        if now >= deadline:
            raise DomainError("EXECUTION_EXPIRED", "Execution deadline elapsed", 409)
        (
            admission.verified_at,
            admission.execution_deadline,
            admission.verified_fingerprint,
            admission.verification_kind,
        ) = now, deadline, record.fingerprint, kind
        record.state, record.execution_deadline = "queued", deadline
        CommandQueueRepository(session).add_job(record.id, now)
        self.audit(
            session,
            context,
            "command.queued",
            record.id,
            execute_by=deadline.isoformat(),
            verification_kind=kind,
        )

    def confirm(self, context: CommandIntakeContext, confirmation: ConfirmCommand) -> IntakeResult:
        fingerprint = digest(
            {
                "kind": "confirm",
                "command_id": str(confirmation.command_id),
                "proof": hashlib.sha256(confirmation.proof.get_secret_value().encode()).hexdigest(),
            }
        )

        def accept(session: Session) -> CommandOutcome:
            record = CommandQueueRepository(session).command(
                str(confirmation.command_id), lock=True
            )
            if record is None:
                raise DomainError("PERMISSION_DENIED", "Confirmation is invalid", 403)
            admission, _, _, _, _ = self.current_request(session, context, record)
            if not admission.confirmation_required:
                raise DomainError("CONFLICT", "Command did not request confirmation", 409)
            if record.state != "awaiting_confirmation":
                if not hmac.compare_digest(
                    self.commands._proof(record).encode(),
                    confirmation.proof.get_secret_value().encode(),
                ):
                    raise DomainError("PERMISSION_DENIED", "Confirmation is invalid", 403)
                return self.outcome(session, context, record)
            if (
                admission.confirmation_key_id != self.confirmation_key_id
                or admission.confirmation_key_tag is None
                or not hmac.compare_digest(
                    admission.confirmation_key_tag.encode(), self.key_tag().encode()
                )
            ):
                raise DomainError(
                    "CONFIRMATION_KEY_CHANGED", "Start a new confirmation intent", 409
                )
            if not hmac.compare_digest(
                self.commands._proof(record).encode(),
                confirmation.proof.get_secret_value().encode(),
            ):
                raise DomainError("PERMISSION_DENIED", "Confirmation is invalid", 403)
            outcome = self.commands._existing(session, context, record)
            if isinstance(outcome, ExpiredCommand):
                return outcome
            self._queue(session, context, record, admission, "proof")
            session.flush()
            return self.outcome(session, context, record)

        return self._run(context, "confirm", fingerprint, accept)

    def withdraw(self, context: CommandIntakeContext, withdrawal: WithdrawCommand) -> IntakeResult:
        fingerprint = digest({"kind": "withdraw", "command_id": str(withdrawal.command_id)})

        def accept(session: Session) -> CommandOutcome:
            repo = CommandQueueRepository(session)
            job_id = repo.job_reference(str(withdrawal.command_id), str(context.actor_id))
            job = repo.job(job_id, lock=True) if job_id is not None else None
            record = repo.command(str(withdrawal.command_id), lock=True)
            if record is None:
                raise DomainError("PERMISSION_DENIED", "Command is not available", 403)
            self._admission(session, context, record)
            if record.state == "succeeded":
                raise DomainError(
                    "COMMAND_ALREADY_FINISHED", "Executed command cannot be withdrawn", 409
                )
            if record.state in {"expired", "failed", "cancelled"}:
                return self.commands._existing(session, context, record)
            if record.state == "queued" and job is None:
                raise DomainError("INTAKE_BUSY", "Command was queued; retry withdrawal", 503)
            if record.state not in {"awaiting_confirmation", "queued"}:
                raise DomainError("CONFLICT", "Command cannot be withdrawn", 409)
            now = self.commands._now()
            record.state, record.completed_at = "cancelled", now
            if job is not None:
                job.state, job.completed_at, job.lease_token, job.lease_expires_at = (
                    "cancelled",
                    now,
                    None,
                    None,
                )
            self.audit(session, context, "command.withdrawn", record.id)
            return CancelledCommand(command_id=record.id)

        return self._run(context, "withdraw", fingerprint, accept)

    def inspect(self, context: DurableActorContext, command_id: UUID) -> CommandOutcome:
        return self.review(context, command_id).outcome

    def review(self, context: DurableActorContext, command_id: UUID) -> CommandReview:
        try:
            with self.sessions.begin() as session:
                self._budget(session)
                self.identity(session, context)
                record = CommandQueueRepository(session).command(str(command_id), lock=True)
                if record is None:
                    raise DomainError("PERMISSION_DENIED", "Command is not available", 403)
                admission, _, _, _, request = self.current_request(session, context, record)
                return CommandReview(
                    command_id=record.id,
                    request=request,
                    outcome=self._validated_outcome(session, context, record, admission),
                )
        except DBAPIError:
            raise DomainError(
                "INTAKE_UNAVAILABLE", "Command outcome unavailable; retry later", 503
            ) from None
