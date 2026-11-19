import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.scheduler.schemas import canonical
from aris.core.time import utc
from aris.modules.nova.report_calendar import WeeklyReportPlan, weekly_report_period
from aris.modules.nova.weekly_plan_models import WeeklyPlanRow
from aris.modules.nova.weekly_plan_schemas import (
    StoredWeeklyPlan,
    plan_fingerprint,
    plan_id,
    plan_key,
)


def inconsistent() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored weekly plan is inconsistent", 503)


class WeeklyPlanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_identity(self, id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.nova.weekly-plan-lock\x00" + id.bytes).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def get(self, id: UUID, *, lock: bool = False) -> StoredWeeklyPlan | None:
        statement = select(*WeeklyPlanRow.__table__.c).where(WeeklyPlanRow.id == str(id))
        if lock:
            statement = statement.with_for_update()
        row = self.session.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        try:
            raw = row["canonical_plan"]
            if len(raw.encode()) > 16384:
                raise ValueError("oversized plan")
            plan = WeeklyReportPlan.model_validate_json(raw)
            if canonical(plan.model_dump(mode="json")) != raw or (
                str(plan_id(plan)),
                plan_key(plan),
                plan.scope.id,
                str(plan.owner_id),
                plan.report_type,
                plan.first_week,
                plan_fingerprint(plan),
            ) != (
                row["id"],
                row["plan_key"],
                row["node_id"],
                row["owner_id"],
                row["report_type"],
                row["first_week"],
                row["fingerprint"],
            ):
                raise ValueError("plan definition metadata differs")
            return StoredWeeklyPlan(
                plan=plan,
                revision=row["revision"],
                created_at=utc(row["created_at"]),
                updated_at=utc(row["updated_at"]),
                next_week=row["next_week"],
                next_due_at=utc(row["next_due_at"]),
                lease_token=row["lease_token"],
                lease_expires_at=utc(row["lease_expires_at"]) if row["lease_expires_at"] else None,
                last_prepared_at=utc(row["last_prepared_at"]) if row["last_prepared_at"] else None,
                last_error_code=row["last_error_code"],
                retry_at=utc(row["retry_at"]) if row["retry_at"] else None,
                consecutive_failures=row["consecutive_failures"],
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def append(self, plan: WeeklyReportPlan, now: datetime) -> None:
        self.session.add(
            WeeklyPlanRow(
                id=str(plan_id(plan)),
                plan_key=plan_key(plan),
                node_id=plan.scope.id,
                owner_id=str(plan.owner_id),
                report_type=plan.report_type,
                first_week=plan.first_week,
                canonical_plan=canonical(plan.model_dump(mode="json")),
                fingerprint=plan_fingerprint(plan),
                created_at=now,
                updated_at=now,
                revision=1,
                next_week=plan.first_week,
                next_due_at=weekly_report_period(plan, plan.first_week).scheduled_for,
                consecutive_failures=0,
            )
        )

    def update(self, id: UUID, **values: Any) -> None:
        self.session.execute(
            update(WeeklyPlanRow).where(WeeklyPlanRow.id == str(id)).values(**values)
        )

    def for_view(
        self, node_id: str, owner_id: UUID, mode: str, limit: int
    ) -> tuple[StoredWeeklyPlan, ...]:
        row = WeeklyPlanRow
        conditions = [row.node_id == node_id, row.report_type == "usage." + mode]
        if mode == "mine":
            conditions.append(row.owner_id == str(owner_id))
        ids = self.session.scalars(
            select(row.id)
            .where(*conditions)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(limit + 1)
        )
        values = tuple(self.get(UUID(id)) for id in ids)
        if any(value is None for value in values):
            raise inconsistent()
        return tuple(value for value in values if value is not None)
