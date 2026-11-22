from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import RolePermission
from aris.core.contracts import DomainError
from aris.modules.lyra.daily_rollup_models import DailyUsageRollupRow
from aris.modules.lyra.weekly_from_daily import (
    DailyWindowReference,
    WeeklyFromDailyQuery,
    WeeklyFromDailyReader,
)
from tests.test_saved_usage_ui import (
    CONTEXT,
    admin_state,
    closed,
    counts,
    coverage,
    derivations,
    history_state,
    incoming_window,
    ledger,
    saved_ui,
    windows,
)

__all__ = [
    "admin_state",
    "closed",
    "coverage",
    "derivations",
    "history_state",
    "ledger",
    "saved_ui",
    "windows",
]


def test_week_builds_from_saved_local_days_without_source_messages(saved_ui, monkeypatch):
    state, _, _, actor, _, ids = saved_ui
    original = incoming_window(state)
    first_day = original.query.start_at.date()
    week = first_day - timedelta(days=first_day.weekday())
    dates = [week + timedelta(days=i) for i in range(7)]
    first = datetime.combine(week, time.min, UTC)
    captured_at = max(state.service._now(), first + timedelta(days=8))
    monkeypatch.setattr(state.service, "clock", lambda: captured_at)
    monkeypatch.setattr(state.service.identities, "clock", lambda: captured_at)
    query = WeeklyFromDailyQuery(node_id="node-a", week=week, timezone="UTC")
    reader = WeeklyFromDailyReader(state.service.reader)
    references = []
    proofs = []
    day_queries = []
    daily = []
    for date in dates:
        start = datetime.combine(date, time.min, UTC)
        window = original.query.model_copy(
            update={"start_at": start, "end_at": start + timedelta(days=1)}
        )
        request = original.model_copy(update={"id": uuid4(), "query": window})
        snapshot = state.service.capture(CONTEXT, request).snapshot
        reference = DailyWindowReference(
            snapshot_id=request.id, snapshot_fingerprint=snapshot.fingerprint
        )
        daily.append(reader.save_day(actor, window, reference))
        references.append(reference)
        day_queries.append(window)
        proofs.append(snapshot.window)

    def only_consolidated(conn, cursor, sql, *args):
        assert "agent_messages" not in sql.lower()
        assert "usage_window_snapshots" not in sql.lower()

    with pytest.raises(DBAPIError), state.service.sessions.begin() as db:
        db.execute(
            update(DailyUsageRollupRow)
            .where(DailyUsageRollupRow.snapshot_id == str(references[0].snapshot_id))
            .values(canonical_rollup="{}")
        )
    before = counts(state.service)
    engine = state.service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", only_consolidated)
    try:
        reader = WeeklyFromDailyReader(state.service.reader)
        assert reader.save_day(actor, day_queries[0], references[0]) == daily[0]
        full = reader.read(actor, query, tuple(references))
        reversed_order = reader.read(actor, query, tuple(reversed(references)))
        assert full == reversed_order
        assert full.missing_days == () and full.coverage == "partial"
        assert full.recorded_microseconds == sum(day.recorded_microseconds for day in proofs) > 0
        assert full.verified_usage_microseconds == sum(
            day.verified_usage_microseconds for day in proofs
        )
        assert full.total_usage_microseconds is None
        assert sum(row.microseconds for row in full.durations) == 7 * 86400 * 1000000
        missing = reader.read(actor, query, tuple(references[:-1]))
        assert missing.missing_days == (dates[-1],) and missing.total_usage_microseconds is None
        with pytest.raises(DomainError) as mismatch:
            reader.read(
                actor, query, (references[0].model_copy(update={"snapshot_fingerprint": "0" * 64}),)
            )
        assert mismatch.value.code == "CONFLICT"
        with state.service.sessions.begin() as db:
            db.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
        with pytest.raises(DomainError) as denied:
            reader.read(actor, query, tuple(references))
        assert denied.value.code == "PERMISSION_DENIED"
    finally:
        event.remove(engine, "before_cursor_execute", only_consolidated)
    assert counts(state.service) == before
