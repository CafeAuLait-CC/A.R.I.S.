from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from aris.core.config import Settings
from aris.core.time import utc
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.target_schemas import GpuInventoryLabel, GpuTargetFact, GpuTargetQuery


class VegaTargetService:
    """Public cross-domain query for a durable GPU target, never live RAM."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

    def gpu(self, query: GpuTargetQuery) -> GpuTargetFact | None:
        with self.sessions() as session:
            return self.gpu_in_transaction(session, query)

    def label_in_transaction(
        self, session: Session, query: GpuTargetQuery
    ) -> GpuInventoryLabel | None:
        """Last-known identity for display; this makes no freshness/slot claim."""
        with session.no_autoflush:
            return VegaRepository(session).gpu_label(query.gpu_uuid)

    def gpu_in_transaction(self, session: Session, query: GpuTargetQuery) -> GpuTargetFact | None:
        """Read inside the command owner's transaction without flushing it."""
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("target fact clock requires a timezone")
        now = utc(now)
        with session.no_autoflush:
            stored = VegaRepository(session).gpu_target(query.gpu_uuid)
        if stored is None:
            return None
        last_seen = utc(stored.last_seen_at)
        observed = utc(stored.observed_at) if stored.observed_at is not None else None
        age = (now - last_seen).total_seconds()
        node_status: Literal["ONLINE", "STALE", "OFFLINE", "UNKNOWN"] = "ONLINE"
        if age < -self.settings.max_clock_skew_seconds:
            node_status = "UNKNOWN"
        elif age >= self.settings.offline_after_seconds:
            node_status = "OFFLINE"
        elif age >= self.settings.stale_after_seconds:
            node_status = "STALE"
        quality: Literal["CURRENT", "STALE", "MISSING", "ERROR"] = "MISSING"
        if observed is not None:
            sample_age = (now - observed).total_seconds()
            quality = "CURRENT"
            if sample_age < -self.settings.max_clock_skew_seconds:
                quality = "ERROR"
            elif sample_age >= self.settings.stale_after_seconds:
                quality = "STALE"
            elif (
                stored.collector_status == "error"
                and stored.collector_health_at is not None
                and utc(stored.collector_health_at) >= observed
            ):
                quality = "ERROR"
        return GpuTargetFact(
            generated_at=now,
            gpu_uuid=stored.gpu_uuid,
            node_id=stored.node_id,
            present=stored.present,
            node_status=node_status,
            telemetry_status=quality,
            observed_at=observed,
            received_at=utc(stored.received_at) if stored.received_at is not None else None,
            last_seen_at=last_seen,
            last_received_at=utc(stored.last_received_at),
            telemetry_seq=stored.telemetry_seq,
        )
