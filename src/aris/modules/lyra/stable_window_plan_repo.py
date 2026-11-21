"""Queries and guarded rolling progress of a stable residency layout."""

import hashlib
import json

from sqlalchemy import LargeBinary, cast, func, select, text, update
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import StableWindowPlanRow
from aris.modules.lyra.stable_window_plan_schemas import (
    MAX_STABLE_PLAN_BYTES,
    StoredStableWindowPlan,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class StableWindowPlanRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, key: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            value = int.from_bytes(
                hashlib.sha256(b"aris.lyra.stable_plan.lock\x00" + key.encode()).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})

    @staticmethod
    def _fields(value: StoredStableWindowPlan) -> dict[str, object]:
        run = value.witness.parent_run
        return dict(
            key=value.key,
            configuration_fingerprint=value.configuration.fingerprint,
            method_fingerprint=value.method.fingerprint,
            consumer_id=value.witness.consumer_id,
            node_id=run.first.node_id,
            run_id=run.id,
            canonical_origin=canonical_trace(value.origin),
            event_id=str(value.event_id),
            created_at=value.created_at,
            version=value.version,
            next_window=value.next_window,
            record_revision=value.witness.consumer_revision,
            observed_intervals=run.intervals,
            planned_windows=value.page.total_planned_windows,
            closed=value.witness.state_at_record == "closed",
            updated_at=value.updated_at,
            canonical_plan=canonical_trace(value),
            fingerprint=value.fingerprint,
        )

    def get(self, key: str) -> StoredStableWindowPlan | None:
        body = StableWindowPlanRow.canonical_plan
        size = (
            func.octet_length(body)
            if self.session.get_bind().dialect.name == "postgresql"
            else func.length(cast(body, LargeBinary))
        )
        length = self.session.scalar(select(size).where(StableWindowPlanRow.key == key))
        if length is None:
            return None
        if not 1 <= length <= MAX_STABLE_PLAN_BYTES:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable plan exceeds its storage budget", 503
            )
        row = (
            self.session.execute(
                select(*StableWindowPlanRow.__table__.c).where(StableWindowPlanRow.key == key)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise DomainError("CONFLICT", "Stable plan disappeared during its read", 409)
        try:
            value = StoredStableWindowPlan.model_validate(json.loads(row["canonical_plan"]))
            expected = self._fields(value)
            if any(
                expected[name] != (utc(actual) if name in {"created_at", "updated_at"} else actual)
                for name, actual in row.items()
            ):
                raise ValueError("stable plan metadata differs from its fixed body")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable plan is inconsistent", 503
            ) from None

    def append(self, value: StoredStableWindowPlan) -> None:
        self.session.add(StableWindowPlanRow(**self._fields(value)))

    def update(self, value: StoredStableWindowPlan) -> None:
        changed = self.session.execute(
            update(StableWindowPlanRow)
            .where(
                StableWindowPlanRow.key == value.key,
                StableWindowPlanRow.version == value.version - 1,
            )
            .values(**self._fields(value))
            .returning(StableWindowPlanRow.key)
        ).scalar_one_or_none()
        if changed is None:
            raise DomainError("CONFLICT", "Stable plan version changed", 409)
