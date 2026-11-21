"""Bounded pending-plan candidates and their retained preparation attempt state."""

from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import StableDispatchStateRow, StableWindowPlanRow
from aris.modules.lyra.recording_job_schemas import RecordingWorkSelection
from aris.modules.lyra.stable_dispatch_schemas import StableDispatchState


class StableDispatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def candidates(self, selection: RecordingWorkSelection, now: datetime) -> tuple[str, ...]:
        plan, attempt = StableWindowPlanRow, StableDispatchStateRow
        return tuple(
            self.session.scalars(
                select(plan.key)
                .outerjoin(attempt, attempt.key == plan.key)
                .where(
                    plan.next_window < plan.planned_windows,
                    plan.configuration_fingerprint == selection.recording_configuration_fingerprint,
                    plan.method_fingerprint == selection.stable_method_fingerprint,
                    or_(
                        attempt.key.is_(None),
                        plan.version > attempt.observed_plan_version,
                        and_(attempt.blocked.is_(False), attempt.available_at <= now),
                    ),
                )
                .order_by(
                    func.coalesce(attempt.updated_at, plan.created_at),
                    func.coalesce(attempt.version, 0),
                    plan.key,
                )
                .limit(16)
            )
        )

    def get(self, key: str) -> StableDispatchState | None:
        row = (
            self.session.execute(
                select(*StableDispatchStateRow.__table__.c).where(StableDispatchStateRow.key == key)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            return StableDispatchState.model_validate(
                {
                    **row,
                    **{
                        name: utc(row[name])
                        for name in ("created_at", "updated_at", "available_at")
                    },
                }
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable dispatch state is inconsistent", 503
            ) from None

    def put(self, value: StableDispatchState, prior: StableDispatchState | None) -> None:
        fields = {**value.model_dump(), "event_id": str(value.event_id)}
        if prior is None:
            self.session.add(StableDispatchStateRow(**fields))
        else:
            changed = self.session.execute(
                update(StableDispatchStateRow)
                .where(
                    StableDispatchStateRow.key == value.key,
                    StableDispatchStateRow.version == prior.version,
                )
                .values(**fields)
                .returning(StableDispatchStateRow.key)
            ).scalar_one_or_none()
            if changed is None:
                raise DomainError("CONFLICT", "Stable dispatch attempt version changed", 409)
