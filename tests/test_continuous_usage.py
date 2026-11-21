import csv
import io
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.core.access.models import RolePermission
from aris.interfaces.slack.config import SlackSettings
from aris.modules.lyra.continuous_usage import UsageRowContext, continuous_usage_history
from aris.modules.lyra.ledger_schemas import PostedMeasurement
from tests.test_recorded_usage import BASE, measured, reversed_measurement
from tests.test_usage_history import (
    TOKEN,
    admin_state,
    derivations,
    history_state,
    input_query,
    ledger,
    readings,
    reduce,
    surface_for,
)
from tests.test_usage_ledger import POST, post_window

__all__ = ["admin_state", "derivations", "history_state", "ledger", "readings"]


def contexts(entries):
    return tuple(
        UsageRowContext(entry.id, entry.fingerprint, entry.details.measurement.timing_fingerprint)
        for entry in entries
        if isinstance(entry.details, PostedMeasurement)
    )


def test_continuous_view_joins_only_exact_matching_recorded_spans():
    entries = [measured(start=0, end=10)]
    for values in (
        {"start": 10, "end": 20},
        {"start": 25, "end": 30},
        {"start": 30, "end": 40, "timing_fingerprint": "c" * 64},
        {"start": 40, "end": 50, "owner_id": uuid4()},
        {"start": 50, "end": 60, "gpu_uuid": "GPU-b"},
        {"start": 60, "end": 70, "gpu_uuid": "GPU-b", "model": "other model"},
        {"start": 70, "end": 80, "gpu_uuid": "GPU-b", "model": "other model"},
    ):
        entries.append(measured(previous=entries[-1], **values))
    history = reduce(tuple(entries))
    original = history.model_dump_json()
    view = continuous_usage_history(history, contexts(entries))
    assert [row.source_segment_count for row in view.intervals] == [2, 1, 1, 1, 1, 2]
    assert sum(row.recorded_microseconds for row in view.intervals) == 75000000
    assert view.history.model_dump_json() == original
    assert view.history.total_usage_microseconds is None and not view.history.quota_usage_available
    assert view.intervals[0].first_entry_id == entries[0].id
    assert view.intervals[0].last_entry_id == entries[1].id
    with pytest.raises(ValueError, match="timing context"):
        continuous_usage_history(history, contexts(entries)[1:])


def test_reversed_time_remains_a_gap_and_the_old_snapshot_stays_continuous():
    a = measured(start=0, end=10)
    b = measured(previous=a, start=10, end=20)
    c = measured(previous=b, start=20, end=30)
    old = reduce((a, b, c))
    reversal = reversed_measurement(b, previous=c)
    current = reduce((a, b, c, reversal))
    assert len(continuous_usage_history(old, contexts((a, b, c))).intervals) == 1
    view = continuous_usage_history(current, contexts((a, b, c, reversal)))
    assert len(view.intervals) == 2 and view.history.recorded_microseconds == 20000000
    assert view.intervals[0].end_at < view.intervals[1].start_at


def test_continuous_http_pages_and_full_exports_group_before_pagination(readings, settings):
    state, _, _ = readings
    surface, _, ids = surface_for(readings, settings)
    for index in range(25):
        state[0].post(POST, post_window(state, start=40 + index * 30, seq=101 + index * 10))
    state[0].post(POST, post_window(state, start=820, seq=501))
    query = input_query(
        presentation="continuous",
        limit=1,
        window={
            "kind": "utc",
            "start_at": BASE.isoformat(),
            "end_at": (BASE + timedelta(seconds=860)).isoformat(),
            "timezone": "UTC",
        },
    ).model_dump(mode="json")
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface)
    ) as client:
        response = client.post("/internal/usage/query", headers=headers, json=query)
        assert response.status_code == 200
        first = response.json()
        assert first["report"]["segment_count"] == 27 and first["report"]["rows"] == []
        assert first["interval_count"] == 2 and len(first["intervals"]) == 1 and first["has_more"]
        assert first["intervals"][0]["source_segment_count"] == 26
        fixed = {**query, "snapshot": first["report"]["snapshot"], "offset": 1}
        # Late recorded evidence fills the gap only in a new ledger snapshot.
        state[0].post(POST, post_window(state, start=790, seq=601))
        second = client.post("/internal/usage/query", headers=headers, json=fixed).json()
        assert second["report"]["fingerprint"] == first["report"]["fingerprint"]
        assert second["interval_count"] == 2 and not second["has_more"]
        assert second["intervals"][0]["source_segment_count"] == 1
        merged = client.post("/internal/usage/export", headers=headers, json=fixed)
        assert merged.status_code == 200
        rows = list(csv.DictReader(io.StringIO(merged.content.decode("utf-8-sig"))))
        assert len(rows) == 2 and sum(int(row["source_segment_count"]) for row in rows) == 27
        assert sum(int(row["recorded_microseconds"]) for row in rows) == 810000000
        raw = client.post(
            "/internal/usage/export", headers=headers, json={**fixed, "presentation": "segments"}
        )
        raw_rows = list(csv.DictReader(io.StringIO(raw.content.decode("utf-8-sig"))))
        assert len(raw_rows) == 27 and {row["report_fingerprint"] for row in raw_rows} == {
            first["report"]["fingerprint"]
        }
        html = client.post("/internal/usage/report", headers=headers, json=fixed)
        assert (
            html.status_code == 200
            and "连续使用时段" in html.text
            and "来自 27 个原始记账分段" in html.text
        )
        latest = client.post("/internal/usage/query", headers=headers, json=query).json()
        assert latest["interval_count"] == 1 and latest["report"]["segment_count"] == 28
        with surface.history.sessions.begin() as db:
            db.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
        denied = client.post("/internal/usage/query", headers=headers, json=query)
        assert denied.status_code == 403
        own = client.post("/internal/usage/query", headers=headers, json={**query, "mode": "mine"})
        assert own.status_code == 200 and own.json()["interval_count"] == 1
