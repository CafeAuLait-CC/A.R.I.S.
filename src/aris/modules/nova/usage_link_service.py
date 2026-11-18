"""Trusted internal association contract; adapters must authorize any disclosure."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import AwareDatetime, TypeAdapter
from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.availability_schemas import AvailabilityResult, MaintenanceCoverage
from aris.modules.nova.reservation_schemas import CreationEvidence, ReservationView
from aris.modules.nova.usage_link_repo import ReservationUsageLinkRepository, UsageLinkHistory
from aris.modules.nova.usage_link_schemas import (
    ReservationHistoryReference,
    ReservationUsageLink,
    ReservationUsageQuery,
)

TIME = TypeAdapter(AwareDatetime)


def _hash(value: object, frame: bytes) -> str:
    return hashlib.sha256(
        frame + json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReservationUsageLinkService:
    def __init__(
        self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.sessions, self.clock = sessions, clock or (lambda: datetime.now(UTC))

    def resolve(self, query: ReservationUsageQuery) -> ReservationUsageLink:
        with self.sessions() as session:
            return self.resolve_in_transaction(session, query)

    @staticmethod
    def _validated_history(
        rows: tuple[UsageLinkHistory, ...], query: ReservationUsageQuery
    ) -> tuple[tuple[UsageLinkHistory, ReservationView, ReservationHistoryReference], ...]:
        try:
            origin = CreationEvidence.model_validate(rows[0].payload)
            initial = origin.reservation
            owner = UUID(rows[0].owner_id)
            if (
                initial.id != query.reservation_id
                or str(initial.gpu_uuid) != rows[0].gpu_uuid
                or UUID(str(rows[0].actor)) != owner
                or initial.created_at > initial.updated_at
                or initial.created_at > initial.start_at
            ):
                raise ValueError("invalid creation metadata")
            result = []
            previous = None
            for index, row in enumerate(rows):
                body = row.payload
                if body is None or row.occurred_at is None:
                    raise ValueError("missing reservation event")
                view = ReservationView.model_validate(body["reservation"])
                occurred = utc(row.occurred_at)
                if (
                    row.version != index + 1
                    or view.version != row.version
                    or view.id != initial.id
                    or view.gpu_uuid != initial.gpu_uuid
                    or view.start_at != initial.start_at
                    or view.purpose != initial.purpose
                    or view.created_at != initial.created_at
                    or view.updated_at != occurred
                    or view.policy_version != initial.policy_version
                ):
                    raise ValueError("reservation history metadata differs")
                UUID(str(body["command_id"]))
                if not isinstance(body["request_id"], str) or not body["request_id"]:
                    raise ValueError("missing request evidence")
                administrative = body.get("administrative", False)
                if (
                    type(administrative) is not bool
                    or (not administrative and UUID(str(row.actor)) != owner)
                    or (administrative and UUID(str(body.get("owner_id"))) != owner)
                ):
                    raise ValueError("reservation owner metadata differs")
                if previous is None:
                    if row.event_type != "reservation.created" or view.status != "confirmed":
                        raise ValueError("missing creation event")
                else:
                    old = previous
                    if occurred < old.updated_at or old.status not in {"confirmed", "active"}:
                        raise ValueError("reservation history time or terminal status differs")
                    if row.event_type == "reservation.cancelled":
                        if (
                            view.status != "cancelled"
                            or old.status != "confirmed"
                            or body.get("previous_status") != old.status
                            or occurred >= old.start_at
                            or view.end_at != old.end_at
                        ):
                            raise ValueError("invalid cancellation")
                    elif row.event_type == "reservation.released":
                        if (
                            view.status != "completed"
                            or body.get("previous_status") != old.status
                            or occurred < old.start_at
                            or view.end_at != old.end_at
                            or TIME.validate_python(body["effective_end_at"])
                            != min(occurred, old.end_at)
                            or TIME.validate_python(body["released_at"]) != occurred
                        ):
                            raise ValueError("invalid release")
                    elif row.event_type == "reservation.extended":
                        decision = AvailabilityResult.model_validate(body["assessment"])
                        maintenance = MaintenanceCoverage.model_validate(
                            body["maintenance_snapshot"]
                        )
                        if (
                            view.status != old.status
                            or view.end_at <= old.end_at
                            or occurred >= old.end_at
                            or TIME.validate_python(body["previous_end_at"]) != old.end_at
                            or decision.code != "AVAILABLE"
                            or decision.resource is None
                            or decision.resource.gpu_uuid != view.gpu_uuid
                            or origin.assessment.resource is None
                            or decision.resource.node_id != origin.assessment.resource.node_id
                            or body.get("node_id") != decision.resource.node_id
                            or body.get("version") != row.version
                            or body.get("assessment_scope") != "extension_delta"
                            or TIME.validate_python(body["end_at"]) != view.end_at
                            or decision.query.gpu_uuid != view.gpu_uuid
                            or decision.query.start_at != old.end_at
                            or decision.query.end_at != view.end_at
                            or decision.generated_at != occurred
                            or decision.rules_fingerprint != body["rules_fingerprint"]
                            or maintenance.fingerprint != body["maintenance_fingerprint"]
                            or maintenance.gpu_uuid != view.gpu_uuid
                            or maintenance.coverage.start_at > old.end_at
                            or maintenance.coverage.end_at < view.end_at
                            or any(
                                window.start_at < view.end_at and window.end_at > old.end_at
                                for window in maintenance.windows
                            )
                        ):
                            raise ValueError("invalid extension")
                    else:
                        raise ValueError("unsupported reservation history event")
                source = dict(
                    id=row.id,
                    reservation_id=str(initial.id),
                    version=row.version,
                    event_type=row.event_type,
                    actor=row.actor,
                    occurred_at=occurred.isoformat(),
                    payload=body,
                )
                reference = ReservationHistoryReference(
                    event_id=row.id,
                    reservation_id=initial.id,
                    version=row.version,
                    event_type=row.event_type,
                    occurred_at=occurred,
                    fingerprint=_hash(source, b"aris.nova.reservation.history\x00"),
                )
                result.append((row, view, reference))
                previous = view
            latest = result[-1][1]
            if (latest.start_at, latest.end_at, latest.status, latest.updated_at) != (
                utc(rows[0].start_at),
                utc(rows[0].end_at),
                rows[0].status,
                utc(rows[0].updated_at),
            ):
                raise ValueError("reservation projection differs from history")
            return tuple(result)
        except (ValueError, TypeError, KeyError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Reservation association history is invalid", 503
            ) from None

    def resolve_in_transaction(
        self, session: Session, query: ReservationUsageQuery
    ) -> ReservationUsageLink:
        query = ReservationUsageQuery.model_validate(query.model_dump(mode="json"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("reservation association clock requires a timezone")
        if query.as_of > now:
            return ReservationUsageLink(query=query, state="unknown", reason="AS_OF_NOT_REACHED")
        with session.no_autoflush:
            rows = ReservationUsageLinkRepository(session).history(str(query.reservation_id))
        if not rows:
            return ReservationUsageLink(
                query=query, state="unknown", reason="RESERVATION_NOT_FOUND"
            )
        validated = self._validated_history(rows, query)
        selected = tuple(item for item in validated if item[2].occurred_at <= query.as_of)
        if not selected:
            return ReservationUsageLink(
                query=query, state="unknown", reason="NO_RESERVATION_AT_CUTOFF"
            )
        _, view, _ = selected[-1]
        if view.version != query.expected_version:
            raise DomainError("CONFLICT", "Reservation history version changed", 409)
        evidence = tuple(item[2] for item in selected)
        fingerprint = _hash(
            [ref.model_dump(mode="json") for ref in evidence],
            b"aris.nova.reservation.association\x00",
        )
        if (
            query.expected_history_fingerprint is not None
            and fingerprint != query.expected_history_fingerprint
        ):
            raise DomainError("CONFLICT", "Reservation history fingerprint changed", 409)
        reason = "OWNER_RESOURCE_AND_WINDOW_MATCH"
        if UUID(rows[0].owner_id) != query.owner_id or view.gpu_uuid != query.gpu_uuid:
            reason = "OWNER_OR_RESOURCE_DIFFERS"
        elif view.status == "cancelled":
            reason = "CANCELLED_RESERVATION"
        else:
            final_body = selected[-1][0].payload
            assert final_body is not None  # verified for every event above
            effective_end = (
                TIME.validate_python(final_body["effective_end_at"])
                if view.status == "completed"
                else view.end_at
            )
            if not view.start_at <= query.start_at < query.end_at <= effective_end:
                reason = "WINDOW_NOT_COVERED"
        result = ReservationUsageLink(
            query=query,
            state="matched" if reason == "OWNER_RESOURCE_AND_WINDOW_MATCH" else "not_matched",
            reason=reason,
            history_fingerprint=fingerprint,
            evidence=evidence,
        )
        with session.no_autoflush:
            if ReservationUsageLinkRepository(session).history(str(query.reservation_id)) != rows:
                raise DomainError("CONFLICT", "Reservation history changed during association", 409)
        return result
