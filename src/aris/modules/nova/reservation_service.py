from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import AvailabilityQuery, AvailabilityResult
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.reservation_schemas import (
    CancelReservation,
    CreatedReservation,
    CreateReservation,
    CreationEvidence,
    MineCursor,
    MineQuery,
    MineResult,
    ReleasedReservation,
    ReleaseReservation,
    ReservationView,
)
from aris.modules.vega.target_schemas import GpuTargetQuery


@contextmanager
def atomic_reservation_write(session: Session) -> Iterator[None]:
    try:
        with transaction_savepoint(session):
            yield
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None)
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if sqlstate == "23P01" and constraint == "ex_reservation_gpu_interval":
            raise DomainError("CONFLICT", "RESERVATION_OVERLAP", 409) from None
        if sqlstate in {"40P01", "40001"}:
            raise DomainError(
                "RETRYABLE_CONFLICT", "Retry the complete command with the same key", 503
            ) from None
        raise


class ReservationService:
    """Internal self-service actions; no application adapter is enabled by default."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        availability: AvailabilityService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.availability = sessions, availability
        self.clock = clock or availability.clock
        self.access = AccessService(sessions)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reservation clock requires a timezone")
        return utc(now)

    def target_scope(self, session: Session, gpu_uuid: str) -> AccessScope:
        with session.no_autoflush:
            fact = self.availability.targets.gpu_in_transaction(
                session, GpuTargetQuery(gpu_uuid=gpu_uuid)
            )
        if fact is None:
            raise DomainError("NOT_FOUND", "Reservation target is unknown", 404)
        if fact.gpu_uuid != gpu_uuid:
            raise DomainError("INTERNAL_ERROR", "Resource target contract differs", 500)
        return AccessScope(type="node", id=fact.node_id)

    def _authorize(
        self, session: Session, execution: CommandExecution, gpu_uuid: str
    ) -> UserPrincipal:
        scope = self.target_scope(session, gpu_uuid)
        if scope != execution.scope:
            raise DomainError("PERMISSION_DENIED", "Reservation target scope differs", 403)
        return self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id,
                permission="reservation.create",
                scope=scope,
            ),
        )

    @staticmethod
    def _require_available(assessment: AvailabilityResult, scope: AccessScope) -> None:
        if assessment.resource is not None and assessment.resource.node_id != scope.id:
            raise DomainError("PERMISSION_DENIED", "Reservation target scope differs", 403)
        if assessment.code == "AVAILABLE":
            return
        status = {
            "CONFLICT": 409,
            "STALE_DATA": 409,
            "POLICY_REJECTED": 409,
            "NOT_EVALUATED": 503,
            "NOT_FOUND": 404,
        }[assessment.code]
        raise DomainError(assessment.code, assessment.reason, status)

    def create_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: CreateReservation,
    ) -> CreatedReservation:
        if principal.user_id != execution.actor_id:
            raise DomainError("PERMISSION_DENIED", "Reservation actor differs", 403)
        with atomic_reservation_write(session):
            return self._create(session, execution, request)

    def _create(
        self, session: Session, execution: CommandExecution, request: CreateReservation
    ) -> CreatedReservation:
        repo = ReservationRepository(session)
        repo.lock_target(request.gpu_uuid)
        self._authorize(session, execution, request.gpu_uuid)
        query = AvailabilityQuery(
            gpu_uuid=request.gpu_uuid, start_at=request.start_at, end_at=request.end_at
        )
        first = self.availability.assess_in_transaction(session, query)
        self._require_available(first, execution.scope)
        reservation_id = str(uuid4())
        reservation = Reservation(
            id=reservation_id,
            owner_id=str(execution.actor_id),
            gpu_uuid=request.gpu_uuid,
            start_at=request.start_at,
            end_at=request.end_at,
            status="confirmed",
            purpose=request.purpose,
            policy_version=None,
            created_at=first.generated_at,
            updated_at=first.generated_at,
        )
        repo.add(reservation)
        session.flush()
        # INSERT/exclusion checks can wait for a noncooperating writer. Check
        # current authority and facts again before appending success evidence.
        self._authorize(session, execution, request.gpu_uuid)
        final = self.availability._assess_in_transaction(
            session, query, exclude_reservation_id=reservation_id
        )
        self._require_available(final, execution.scope)
        maintenance = self.availability.maintenance_snapshot(request.gpu_uuid)
        if maintenance is None or final.rules_fingerprint is None:
            raise DomainError("NOT_EVALUATED", "Decision configuration changed", 503)
        maintenance_fingerprint = maintenance.fingerprint
        reservation.updated_at = final.generated_at
        view = ReservationView(
            id=reservation_id,
            gpu_uuid=request.gpu_uuid,
            start_at=request.start_at,
            end_at=request.end_at,
            status="confirmed",
            purpose=request.purpose,
            policy_version=None,
            created_at=first.generated_at,
            updated_at=final.generated_at,
            version=1,
        )
        try:
            evidence = CreationEvidence(
                reservation=view,
                assessment_scope="basic_window",
                command_id=execution.command_id,
                request_id=execution.request_id,
                assessment=final,
                maintenance_snapshot=maintenance,
                maintenance_fingerprint=maintenance_fingerprint,
            )
        except ValidationError:
            raise DomainError(
                "INTERNAL_ERROR", "Creation decision evidence is inconsistent", 500
            ) from None
        repo.append_event(
            ReservationEvent(
                id=str(uuid4()),
                reservation_id=reservation_id,
                version=1,
                event_type="reservation.created",
                actor=str(execution.actor_id),
                occurred_at=final.generated_at,
                payload=evidence.model_dump(mode="json"),
            )
        )
        event_id = str(uuid4())
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type="reservation.created",
                event_version=1,
                aggregate_type="reservation",
                aggregate_id=reservation_id,
                aggregate_version=1,
                occurred_at=final.generated_at,
                recorded_at=final.generated_at,
                actor=str(execution.actor_id),
                correlation_id=execution.request_id,
                causation_id=str(execution.command_id),
                payload={
                    "reservation_id": reservation_id,
                    "gpu_uuid": request.gpu_uuid,
                    "start_at": request.start_at.isoformat(),
                    "end_at": request.end_at.isoformat(),
                    "assessment_scope": "basic_window",
                    "rules_fingerprint": final.rules_fingerprint,
                    "maintenance_fingerprint": maintenance_fingerprint,
                },
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=final.generated_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=str(execution.actor_id),
                action="reservation.created",
                target=reservation_id,
                request_id=execution.request_id,
                recorded_at=final.generated_at,
                changes={
                    "command_id": str(execution.command_id),
                    "gpu_uuid": request.gpu_uuid,
                    "node_id": execution.scope.id,
                    "status": "confirmed",
                    "start_at": request.start_at.isoformat(),
                    "end_at": request.end_at.isoformat(),
                    "rules_fingerprint": final.rules_fingerprint,
                },
            )
        )
        session.flush()
        return CreatedReservation(
            reservation=view,
            rules_fingerprint=final.rules_fingerprint,
            maintenance_fingerprint=maintenance_fingerprint,
        )

    def mine(self, actor: ActorContext, query: MineQuery) -> MineResult:
        with self.sessions() as session:
            return self.mine_in_transaction(session, actor, query)

    def mine_in_transaction(
        self, session: Session, actor: ActorContext, query: MineQuery
    ) -> MineResult:
        with session.no_autoflush:
            self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=actor.actor_id,
                    permission="reservation.mine",
                    scope=AccessScope(type="global", id="*"),
                ),
            )
            records = ReservationRepository(session).mine(str(actor.actor_id), query)
        page = records[: query.limit]
        cursor = (
            MineCursor(created_at=page[-1].created_at, reservation_id=page[-1].id)
            if len(records) > query.limit
            else None
        )
        return MineResult(reservations=page, next_cursor=cursor)

    def _cancel_authority_and_time(
        self, session: Session, execution: CommandExecution, row: Reservation
    ) -> datetime:
        global_scope = AccessScope(type="global", id="*")
        if execution.scope != global_scope:
            raise DomainError("PERMISSION_DENIED", "Cancellation scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id, permission="reservation.cancel_own", scope=global_scope
            ),
        )
        now = self._now()
        if now >= utc(row.start_at):
            raise DomainError("CONFLICT", "RESERVATION_ALREADY_STARTED", 409)
        return now

    def cancel_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: CancelReservation,
    ) -> ReservationView:
        if principal.user_id != execution.actor_id:
            raise DomainError("PERMISSION_DENIED", "Reservation actor differs", 403)
        with atomic_reservation_write(session):
            repo = ReservationRepository(session)
            reference = repo.reference(str(request.reservation_id))
            if reference is None or reference[0] != str(execution.actor_id):
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            repo.lock_target(reference[1])
            row = repo.lock_reservation(str(request.reservation_id))
            if row is None or row.owner_id != str(execution.actor_id):
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            if row.gpu_uuid != reference[1]:
                raise DomainError(
                    "RETRYABLE_CONFLICT", "Reservation target changed; retry command", 503
                )
            now = self._cancel_authority_and_time(session, execution, row)
            version = repo.history_version(row.id)
            if version is None:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Reservation history is missing", 503)
            if request.expected_version != version:
                raise DomainError("CONFLICT", "RESERVATION_VERSION_CHANGED", 409)
            if row.status != "confirmed":
                raise DomainError("CONFLICT", "RESERVATION_NOT_CANCELLABLE", 409)
            row.status, row.updated_at = "cancelled", now
            session.flush()
            now = self._cancel_authority_and_time(session, execution, row)
            row.updated_at = now
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
            repo.append_event(
                ReservationEvent(
                    id=str(uuid4()),
                    reservation_id=row.id,
                    version=version + 1,
                    event_type="reservation.cancelled",
                    actor=str(execution.actor_id),
                    occurred_at=now,
                    payload={
                        "previous_status": "confirmed",
                        "reservation": view.model_dump(mode="json"),
                        "reason": request.reason,
                        "command_id": str(execution.command_id),
                        "request_id": execution.request_id,
                    },
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="reservation.cancelled",
                    event_version=1,
                    aggregate_type="reservation",
                    aggregate_id=row.id,
                    aggregate_version=version + 1,
                    occurred_at=now,
                    recorded_at=now,
                    actor=str(execution.actor_id),
                    correlation_id=execution.request_id,
                    causation_id=str(execution.command_id),
                    payload={
                        "reservation_id": row.id,
                        "gpu_uuid": row.gpu_uuid,
                        "previous_status": "confirmed",
                        "status": "cancelled",
                        "version": version + 1,
                    },
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(execution.actor_id),
                    action="reservation.cancelled",
                    target=row.id,
                    request_id=execution.request_id,
                    recorded_at=now,
                    changes={
                        "command_id": str(execution.command_id),
                        "previous_status": "confirmed",
                        "status": "cancelled",
                        "version": version + 1,
                        "reason": request.reason,
                    },
                )
            )
            session.flush()
            return view

    def _release_authority_and_time(
        self, session: Session, execution: CommandExecution, row: Reservation
    ) -> datetime:
        self._release_scope_access(session, execution)
        now = self._now()
        if now < utc(row.start_at):
            raise DomainError("CONFLICT", "RESERVATION_NOT_STARTED", 409)
        return now

    def release_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: ReleaseReservation,
    ) -> ReleasedReservation:
        if principal.user_id != execution.actor_id:
            raise DomainError("PERMISSION_DENIED", "Reservation actor differs", 403)
        with atomic_reservation_write(session):
            repo = ReservationRepository(session)
            self._release_scope_access(session, execution)
            reference = repo.reference(str(request.reservation_id))
            if reference is None or reference[0] != str(execution.actor_id):
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            repo.lock_target(reference[1])
            row = repo.lock_reservation(str(request.reservation_id))
            if row is None or row.owner_id != str(execution.actor_id):
                raise DomainError("NOT_FOUND", "Reservation is not available", 404)
            if row.gpu_uuid != reference[1]:
                raise DomainError(
                    "RETRYABLE_CONFLICT", "Reservation target changed; retry command", 503
                )
            now = self._release_authority_and_time(session, execution, row)
            version = repo.history_version(row.id)
            if version is None:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Reservation history is missing", 503)
            if request.expected_version != version:
                raise DomainError("CONFLICT", "RESERVATION_VERSION_CHANGED", 409)
            if row.status not in {"confirmed", "active"}:
                raise DomainError("CONFLICT", "RESERVATION_NOT_RELEASABLE", 409)
            previous = row.status
            row.status, row.updated_at = "completed", now
            session.flush()
            now = self._release_authority_and_time(session, execution, row)
            row.updated_at = now
            effective_end = min(now, utc(row.end_at))
            timing = {
                "released_at": now.isoformat(),
                "effective_end_at": effective_end.isoformat(),
            }
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
            repo.append_event(
                ReservationEvent(
                    id=str(uuid4()),
                    reservation_id=row.id,
                    version=version + 1,
                    event_type="reservation.released",
                    actor=str(execution.actor_id),
                    occurred_at=now,
                    payload={
                        "previous_status": previous,
                        "reservation": view.model_dump(mode="json"),
                        "reason": request.reason,
                        "command_id": str(execution.command_id),
                        "request_id": execution.request_id,
                        **timing,
                    },
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="reservation.released",
                    event_version=1,
                    aggregate_type="reservation",
                    aggregate_id=row.id,
                    aggregate_version=version + 1,
                    occurred_at=now,
                    recorded_at=now,
                    actor=str(execution.actor_id),
                    correlation_id=execution.request_id,
                    causation_id=str(execution.command_id),
                    payload={
                        "reservation_id": row.id,
                        "gpu_uuid": row.gpu_uuid,
                        "version": version + 1,
                        "previous_status": previous,
                        "status": "completed",
                        **timing,
                    },
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(execution.actor_id),
                    action="reservation.released",
                    target=row.id,
                    request_id=execution.request_id,
                    recorded_at=now,
                    changes={
                        "command_id": str(execution.command_id),
                        "version": version + 1,
                        "previous_status": previous,
                        "status": "completed",
                        "reason": request.reason,
                        **timing,
                    },
                )
            )
            session.flush()
            return ReleasedReservation(
                reservation=view, released_at=now, effective_end_at=effective_end
            )

    def _release_scope_access(self, session: Session, execution: CommandExecution) -> None:
        scope = AccessScope(type="global", id="*")
        if execution.scope != scope:
            raise DomainError("PERMISSION_DENIED", "Release scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id, permission="reservation.release_own", scope=scope
            ),
        )
