from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import CommandExecution
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import AvailabilityQuery, AvailabilityResult
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.repo import ReservationRepository
from aris.modules.nova.reservation_schemas import (
    CreationEvidence,
    ExtendedReservation,
    ExtendReservation,
    ReservationView,
)
from aris.modules.nova.reservation_service import atomic_reservation_write
from aris.modules.vega.target_schemas import GpuTargetQuery


class ReservationExtensionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        availability: AvailabilityService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.availability = sessions, availability
        self.clock, self.access = clock or availability.clock, AccessService(sessions)

    def _personal_access(self, session: Session, execution: CommandExecution) -> None:
        scope = AccessScope(type="global", id="*")
        if execution.scope != scope:
            raise DomainError("PERMISSION_DENIED", "Extension scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id, permission="reservation.extend_own", scope=scope
            ),
        )

    def _node_access(
        self,
        session: Session,
        execution: CommandExecution,
        gpu_uuid: str,
        expected: AccessScope | None = None,
    ) -> AccessScope:
        with session.no_autoflush:
            fact = self.availability.targets.gpu_in_transaction(
                session, GpuTargetQuery(gpu_uuid=gpu_uuid)
            )
        if fact is None:
            raise DomainError("NOT_FOUND", "Reservation target is unknown", 404)
        if fact.gpu_uuid != gpu_uuid:
            raise DomainError("INTERNAL_ERROR", "Resource target contract differs", 500)
        scope = AccessScope(type="node", id=fact.node_id)
        if expected is not None and scope != expected:
            raise DomainError("PERMISSION_DENIED", "Extension target scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(user_id=execution.actor_id, permission="reservation.extend", scope=scope),
        )
        return scope

    def _before_end(self, old_end: datetime) -> None:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("extension clock requires a timezone")
        if utc(now) >= old_end:
            raise DomainError("CONFLICT", "RESERVATION_ALREADY_ENDED", 409)

    @staticmethod
    def _creation_basis(repo: ReservationRepository, row: Reservation) -> None:
        original = repo.creation_basis(row.id)
        if original is not None:
            actor, payload = original
            try:
                evidence = CreationEvidence.model_validate(payload)
            except ValidationError:
                evidence = None
            if evidence is not None and (
                actor == row.owner_id
                and str(evidence.reservation.id) == row.id
                and evidence.reservation.gpu_uuid == row.gpu_uuid
                and evidence.reservation.start_at == utc(row.start_at)
                and evidence.reservation.end_at <= utc(row.end_at)
                and evidence.reservation.purpose == row.purpose
            ):
                return
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Extension creation evidence is invalid", 503)

    @staticmethod
    def _assessment(
        assessment: AvailabilityResult, scope: AccessScope, row: Reservation, new_end: datetime
    ) -> None:
        if assessment.resource is not None and assessment.resource.node_id != scope.id:
            raise DomainError("PERMISSION_DENIED", "Extension target scope differs", 403)
        if assessment.code != "AVAILABLE":
            status = (
                503
                if assessment.code == "NOT_EVALUATED"
                else 404
                if assessment.code == "NOT_FOUND"
                else 409
            )
            raise DomainError(assessment.code, assessment.reason, status)
        rules = assessment.rules_snapshot
        if rules is None:
            raise DomainError("NOT_EVALUATED", "RULES_UNCONFIGURED", 503)
        if (new_end - utc(row.start_at)).total_seconds() > rules.max_duration_seconds:
            raise DomainError("POLICY_REJECTED", "TOTAL_WINDOW_TOO_LONG", 409)

    def extend_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: ExtendReservation,
    ) -> ExtendedReservation:
        if principal.user_id != execution.actor_id:
            raise DomainError("PERMISSION_DENIED", "Reservation actor differs", 403)
        with atomic_reservation_write(session):
            self._personal_access(session, execution)
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
            self._personal_access(session, execution)
            version = repo.history_version(row.id)
            if version is None or row.policy_version is not None:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Extension policy basis is unavailable", 503
                )
            self._creation_basis(repo, row)
            if version != request.expected_version:
                raise DomainError("CONFLICT", "RESERVATION_VERSION_CHANGED", 409)
            if row.status not in {"confirmed", "active"}:
                raise DomainError("CONFLICT", "RESERVATION_NOT_EXTENDABLE", 409)
            old_end = utc(row.end_at)
            if request.new_end_at <= old_end:
                raise DomainError("VALIDATION_ERROR", "Extension must increase the end time", 422)
            self._before_end(old_end)
            scope = self._node_access(session, execution, row.gpu_uuid)
            query = AvailabilityQuery(
                gpu_uuid=row.gpu_uuid, start_at=old_end, end_at=request.new_end_at
            )
            first = self.availability.assess_in_transaction(session, query)
            self._assessment(first, scope, row, request.new_end_at)
            row.end_at, row.updated_at = request.new_end_at, first.generated_at
            session.flush()
            self._personal_access(session, execution)
            self._before_end(old_end)
            self._node_access(session, execution, row.gpu_uuid, expected=scope)
            final = self.availability._assess_in_transaction(
                session, query, exclude_reservation_id=row.id
            )
            self._assessment(final, scope, row, request.new_end_at)
            self._before_end(old_end)
            maintenance = self.availability.maintenance_snapshot(row.gpu_uuid)
            if maintenance is None or final.rules_fingerprint is None:
                raise DomainError("NOT_EVALUATED", "Decision configuration changed", 503)
            maintenance_json = maintenance.model_dump(mode="json")
            maintenance_hash = maintenance.fingerprint
            row.updated_at = final.generated_at
            view = ReservationView(
                id=row.id,
                gpu_uuid=row.gpu_uuid,
                start_at=utc(row.start_at),
                end_at=request.new_end_at,
                status=row.status,
                purpose=row.purpose,
                policy_version=None,
                created_at=utc(row.created_at),
                updated_at=final.generated_at,
                version=version + 1,
            )
            change = {
                "previous_end_at": old_end.isoformat(),
                "end_at": request.new_end_at.isoformat(),
                "node_id": scope.id,
                "version": version + 1,
                "rules_fingerprint": final.rules_fingerprint,
                "maintenance_fingerprint": maintenance_hash,
            }
            repo.append_event(
                ReservationEvent(
                    id=str(uuid4()),
                    reservation_id=row.id,
                    version=version + 1,
                    event_type="reservation.extended",
                    actor=str(execution.actor_id),
                    occurred_at=final.generated_at,
                    payload={
                        "assessment_scope": "extension_delta",
                        "reservation": view.model_dump(mode="json"),
                        "command_id": str(execution.command_id),
                        "request_id": execution.request_id,
                        "reason": request.reason,
                        "assessment": final.model_dump(mode="json"),
                        "maintenance_snapshot": maintenance_json,
                        **change,
                    },
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="reservation.extended",
                    event_version=1,
                    aggregate_type="reservation",
                    aggregate_id=row.id,
                    aggregate_version=version + 1,
                    occurred_at=final.generated_at,
                    recorded_at=final.generated_at,
                    actor=str(execution.actor_id),
                    correlation_id=execution.request_id,
                    causation_id=str(execution.command_id),
                    payload={
                        "reservation_id": row.id,
                        "owner_id": row.owner_id,
                        "administrative": False,
                        "gpu_uuid": row.gpu_uuid,
                        **change,
                    },
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=final.generated_at))
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=str(execution.actor_id),
                    action="reservation.extended",
                    target=row.id,
                    request_id=execution.request_id,
                    recorded_at=final.generated_at,
                    changes={
                        "command_id": str(execution.command_id),
                        "reason": request.reason,
                        **change,
                    },
                )
            )
            session.flush()
            return ExtendedReservation(
                reservation=view,
                previous_end_at=old_end,
                rules_fingerprint=final.rules_fingerprint,
                maintenance_fingerprint=maintenance_hash,
            )
