import hashlib
import json
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import AvailabilityQuery, AvailabilityResult
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.reservation_schemas import (
    CreatedReservation,
    CreateReservation,
    MineCursor,
    MineQuery,
    MineResult,
    ReservationView,
)
from aris.modules.vega.target_schemas import GpuTargetQuery


class ReservationService:
    """Internal self-service actions; no application adapter is enabled by default."""

    def __init__(self, sessions: sessionmaker[Session], availability: AvailabilityService) -> None:
        self.sessions, self.availability = sessions, availability
        self.access = AccessService(sessions)

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
        try:
            with transaction_savepoint(session):
                return self._create(session, execution, request)
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
        maintenance_json = maintenance.model_dump(mode="json")
        maintenance_fingerprint = hashlib.sha256(
            json.dumps(maintenance_json, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
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
        evidence = {
            "assessment_scope": "basic_window",
            "command_id": str(execution.command_id),
            "request_id": execution.request_id,
            "assessment": final.model_dump(mode="json"),
            "maintenance_snapshot": maintenance_json,
            "maintenance_fingerprint": maintenance_fingerprint,
        }
        repo.append_event(
            ReservationEvent(
                id=str(uuid4()),
                reservation_id=reservation_id,
                version=1,
                event_type="reservation.created",
                actor=str(execution.actor_id),
                occurred_at=final.generated_at,
                payload={"reservation": view.model_dump(mode="json"), **evidence},
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
