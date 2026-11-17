from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import CommandExecution
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.reservation_schemas import (
    AdministrativeReservationChange,
    AdministrativeReservationResult,
    AdministrativeReservationState,
    ReservationView,
)
from aris.modules.nova.reservation_service import atomic_reservation_write


class ReservationAdministrationService:
    """Explicit global capacity reductions; no resource or quota override."""

    def __init__(self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime]) -> None:
        self.sessions, self.clock = sessions, clock
        self.access = AccessService(sessions)

    def _authorize(
        self, session: Session, execution: CommandExecution, operation: Literal["cancel", "release"]
    ) -> None:
        scope = AccessScope(type="global", id="*")
        if execution.scope != scope:
            raise DomainError("PERMISSION_DENIED", "Administrative scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id, permission=f"reservation.{operation}_any", scope=scope
            ),
        )

    def _time(self, row: Reservation, operation: Literal["cancel", "release"]) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("administration clock requires a timezone")
        now = utc(now)
        if operation == "cancel" and now >= utc(row.start_at):
            raise DomainError("CONFLICT", "RESERVATION_ALREADY_STARTED", 409)
        if operation == "release" and now < utc(row.start_at):
            raise DomainError("CONFLICT", "RESERVATION_NOT_STARTED", 409)
        return now

    def cancel_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: AdministrativeReservationChange,
    ) -> AdministrativeReservationResult:
        return self._change(session, execution, principal, request, "cancel")

    def release_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: AdministrativeReservationChange,
    ) -> AdministrativeReservationResult:
        return self._change(session, execution, principal, request, "release")

    def _change(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: AdministrativeReservationChange,
        operation: Literal["cancel", "release"],
    ) -> AdministrativeReservationResult:
        if principal.user_id != execution.actor_id:
            raise DomainError("PERMISSION_DENIED", "Administrative actor differs", 403)
        with atomic_reservation_write(session):
            self._authorize(session, execution, operation)
            repo = ReservationRepository(session)
            reference = repo.reference(str(request.reservation_id))
            if reference is None:
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            repo.lock_target(reference[1])
            row = repo.lock_reservation(str(request.reservation_id))
            if row is None:
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            self._authorize(session, execution, operation)
            if row.gpu_uuid != reference[1]:
                raise DomainError(
                    "RETRYABLE_CONFLICT", "Reservation target changed; retry command", 503
                )
            if row.owner_id != str(request.expected_owner_id):
                raise DomainError("CONFLICT", "RESERVATION_OWNER_CHANGED", 409)
            version = repo.history_version(row.id)
            if version is None:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Reservation history is missing", 503)
            if version != request.expected_version:
                raise DomainError("CONFLICT", "RESERVATION_VERSION_CHANGED", 409)
            allowed = {"confirmed"} if operation == "cancel" else {"confirmed", "active"}
            if row.status not in allowed:
                raise DomainError("CONFLICT", "RESERVATION_STATE_CHANGED", 409)
            previous = row.status
            now = self._time(row, operation)
            row.status = "cancelled" if operation == "cancel" else "completed"
            row.updated_at = now
            session.flush()
            self._authorize(session, execution, operation)
            now = self._time(row, operation)
            row.updated_at = now
            effective_end = min(now, utc(row.end_at)) if operation == "release" else None
            timing = (
                {"released_at": now.isoformat(), "effective_end_at": effective_end.isoformat()}
                if effective_end is not None
                else {}
            )
            view = ReservationView(
                id=row.id,
                gpu_uuid=row.gpu_uuid,
                start_at=utc(row.start_at),
                end_at=utc(row.end_at),
                status=row.status,
                purpose=row.purpose,
                policy_version=row.policy_version,
                created_at=utc(row.created_at),
                updated_at=now,
                version=version + 1,
            )
            event_type = (
                "reservation.cancelled" if operation == "cancel" else "reservation.released"
            )
            changes = {
                "owner_id": row.owner_id,
                "administrative": True,
                "previous_status": previous,
                "status": row.status,
                "version": version + 1,
                **timing,
            }
            repo.append_event(
                ReservationEvent(
                    id=str(uuid4()),
                    reservation_id=row.id,
                    version=version + 1,
                    event_type=event_type,
                    actor=str(execution.actor_id),
                    occurred_at=now,
                    payload={
                        "reservation": view.model_dump(mode="json"),
                        "reason": request.reason,
                        "command_id": str(execution.command_id),
                        "request_id": execution.request_id,
                        **changes,
                    },
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type=event_type,
                    event_version=1,
                    aggregate_type="reservation",
                    aggregate_id=row.id,
                    aggregate_version=version + 1,
                    occurred_at=now,
                    recorded_at=now,
                    actor=str(execution.actor_id),
                    correlation_id=execution.request_id,
                    causation_id=str(execution.command_id),
                    payload={"reservation_id": row.id, "gpu_uuid": row.gpu_uuid, **changes},
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(execution.actor_id),
                    action=event_type,
                    target=row.id,
                    request_id=execution.request_id,
                    recorded_at=now,
                    changes={
                        "command_id": str(execution.command_id),
                        "reason": request.reason,
                        **changes,
                    },
                )
            )
            session.flush()
            return AdministrativeReservationResult(
                reservation=AdministrativeReservationState(
                    id=row.id,
                    gpu_uuid=row.gpu_uuid,
                    start_at=utc(row.start_at),
                    end_at=utc(row.end_at),
                    status=row.status,
                    version=version + 1,
                    updated_at=now,
                ),
                owner_id=row.owner_id,
                actor_id=execution.actor_id,
                operation=operation,
                effective_end_at=effective_end,
            )
