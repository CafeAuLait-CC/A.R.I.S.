from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.availability_schemas import (
    CONFLICT_PREVIEW_LIMIT,
    AvailabilityCode,
    AvailabilityQuery,
    AvailabilityResult,
    BasicReservationRules,
    ConflictSummary,
    MaintenanceCoverage,
)
from aris.modules.nova.repo import ReservationRepository
from aris.modules.vega.target_schemas import GpuTargetFact, GpuTargetQuery


class GpuTargets(Protocol):
    def gpu_in_transaction(self, session: Session, query: GpuTargetQuery) -> GpuTargetFact | None:
        """Read the actual target using the caller's transaction."""
        ...


class AvailabilityService:
    """Read-only basic resource/window assessment, not permission or quota approval."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        targets: GpuTargets,
        *,
        rules: BasicReservationRules | None = None,
        maintenance: Sequence[MaintenanceCoverage] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.targets, self.rules = sessions, targets, rules
        self.clock = clock or (lambda: datetime.now(UTC))
        self._maintenance: dict[str, MaintenanceCoverage] = {}
        for snapshot in maintenance:
            if snapshot.gpu_uuid in self._maintenance:
                raise ValueError("duplicate GPU maintenance coverage")
            self._maintenance[snapshot.gpu_uuid] = snapshot

    def assess(self, query: AvailabilityQuery) -> AvailabilityResult:
        with self.sessions() as session:
            return self.assess_in_transaction(session, query)

    def assess_in_transaction(
        self, session: Session, query: AvailabilityQuery
    ) -> AvailabilityResult:
        return self._assess_in_transaction(session, query)

    def maintenance_snapshot(self, gpu_uuid: str) -> MaintenanceCoverage | None:
        return self._maintenance.get(gpu_uuid)

    def _assess_in_transaction(
        self,
        session: Session,
        query: AvailabilityQuery,
        *,
        exclude_reservation_id: str | None = None,
    ) -> AvailabilityResult:
        # Only Nova's provisional-write recheck excludes its own freshly inserted
        # ID. The public read contract always includes every blocking reservation.
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("availability clock requires a timezone")
        now = utc(now)
        with session.no_autoflush:
            resource = self.targets.gpu_in_transaction(
                session, GpuTargetQuery(gpu_uuid=query.gpu_uuid)
            )
            if resource is not None and resource.gpu_uuid != query.gpu_uuid:
                raise DomainError("INTERNAL_ERROR", "Resource target contract differs", 500)

            def result(
                code: AvailabilityCode,
                reason: str,
                conflicts: ConflictSummary | None = None,
                maintenance: ConflictSummary | None = None,
            ) -> AvailabilityResult:
                return AvailabilityResult(
                    generated_at=now,
                    query=query,
                    code=code,
                    reason=reason,
                    available=True
                    if code == "AVAILABLE"
                    else None
                    if code in {"NOT_EVALUATED", "STALE_DATA"}
                    else False,
                    resource=resource,
                    rules_snapshot=self.rules,
                    rules_fingerprint=self.rules.fingerprint if self.rules else None,
                    reservation_conflicts=conflicts,
                    maintenance_conflicts=maintenance,
                )

            if resource is None:
                return result("NOT_FOUND", "TARGET_UNKNOWN")
            if resource.node_status != "ONLINE":
                return result("STALE_DATA", "NODE_NOT_CURRENT")
            if resource.telemetry_status != "CURRENT":
                return result("STALE_DATA", "GPU_NOT_CURRENT")
            if not resource.present:
                return result("NOT_FOUND", "TARGET_ABSENT")
            if self.rules is None:
                return result("NOT_EVALUATED", "RULES_UNCONFIGURED")
            if (query.end_at - query.start_at).total_seconds() > self.rules.max_duration_seconds:
                return result("POLICY_REJECTED", "WINDOW_TOO_LONG")
            notice = (query.start_at - now).total_seconds()
            if notice < self.rules.min_notice_seconds:
                return result("POLICY_REJECTED", "NOTICE_TOO_SHORT")
            if notice > self.rules.max_advance_seconds:
                return result("POLICY_REJECTED", "TOO_FAR_AHEAD")
            coverage = self._maintenance.get(query.gpu_uuid)
            if coverage is None:
                return result("NOT_EVALUATED", "MAINTENANCE_UNCONFIGURED")
            if (
                coverage.coverage.start_at > query.start_at
                or coverage.coverage.end_at < query.end_at
            ):
                return result("NOT_EVALUATED", "MAINTENANCE_NOT_COVERED")
            conflicts = ReservationRepository(session).conflict_summary(
                query.gpu_uuid, query, exclude_reservation_id=exclude_reservation_id
            )
            windows = tuple(
                sorted(
                    (
                        item
                        for item in coverage.windows
                        if item.start_at < query.end_at and item.end_at > query.start_at
                    ),
                    key=lambda item: (item.start_at, item.end_at),
                )
            )
            maintenance = ConflictSummary(
                total=len(windows),
                windows=windows[:CONFLICT_PREVIEW_LIMIT],
                truncated=len(windows) > CONFLICT_PREVIEW_LIMIT,
            )
            if conflicts.total:
                return result("CONFLICT", "RESERVATION_OVERLAP", conflicts, maintenance)
            if maintenance.total:
                return result("POLICY_REJECTED", "MAINTENANCE_OVERLAP", conflicts, maintenance)
            return result("AVAILABLE", "BASIC_WINDOW_CHECKS_PASSED", conflicts, maintenance)
