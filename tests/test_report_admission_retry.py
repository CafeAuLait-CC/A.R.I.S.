from datetime import UTC, datetime, timedelta

from aris.apps.ops.weekly_coverage import _periodic_wait_seconds


def test_active_source_wait_catches_short_quiet_window_without_polling_future_days():
    current = datetime(2026, 11, 25, 7, 5, tzinfo=UTC)

    def delay(state, **fields):
        return _periodic_wait_seconds({"plans": [{"state": state, **fields}]}, lambda: current, 60)

    now = 0
    attempts = []
    while not 2 <= now < 5 and now < 61:
        attempts.append(now)
        now += delay("waiting", error_code="COVERAGE_SOURCE_NOT_CAUGHT_UP")
    assert now == 2 and attempts == [0, 1]
    assert delay("waiting", error_code="COVERAGE_BUILD_PENDING") == 60
    assert delay("failed", error_code="DEPENDENCY_UNAVAILABLE") == 60
    assert delay("future_day", due_at=(current + timedelta(hours=6)).isoformat()) == 21600
    assert delay("caught_up", next_due_at=(current + timedelta(days=1)).isoformat()) == 86400
