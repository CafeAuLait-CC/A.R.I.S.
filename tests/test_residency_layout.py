import pytest
from pydantic import ValidationError

from aris.modules.lyra.residency_layout import (
    ResidencyLayoutPage,
    StableResidencyLayoutMethod,
    plan_residency_windows,
    residency_layout_counts,
)
from tests.test_residency_observed_windows import consumer, observed, partitioned
from tests.test_residency_partitions import verifier
from tests.test_residency_segment_store import counts

__all__ = ["consumer", "partitioned"]


def test_growth_keeps_prefix_boundaries_and_reserves_bounded_nonempty_final_tail():
    for capacity in (1, 2, 3, 7, 128):
        previous = 0
        for intervals in (*range(1, 514), 9223372036854775807):
            opened = residency_layout_counts(intervals, "open", capacity)
            closed = residency_layout_counts(intervals, "closed", capacity)
            prefix, total, tail = opened
            assert total == prefix >= previous
            assert tail == intervals - prefix * capacity
            assert 1 <= tail < 2 * capacity
            if intervals >= capacity:
                assert tail >= capacity
            assert closed[0] == prefix and closed[2] == 0
            assert closed[1] - prefix in (1, 2)
            if closed[1] - prefix == 2:
                # The final pieces remain bounded without discarding a single interval.
                assert tail // 2 + (tail + 1) // 2 == tail
                assert 1 <= tail // 2 <= (tail + 1) // 2 <= capacity
            previous = prefix
    for intervals, state, capacity in (
        (True, "open", 4),
        (0, "open", 4),
        (9, "other", 4),
        (9, "open", 129),
    ):
        with pytest.raises(ValueError):
            residency_layout_counts(intervals, state, capacity)


def test_fixed_layout_pages_bind_original_proofs_keep_stable_keys_and_do_not_promote_short_windows(
    partitioned,
):
    state, ref, proofs = partitioned
    before = counts(state)
    app = verifier(state)
    opened = app.observed_window(observed(state, ref, proofs[0]))
    closed = app.observed_window(observed(state, ref, proofs[-1]))
    method = StableResidencyLayoutMethod(max_intervals=1)
    first = plan_residency_windows(opened, method)
    last = plan_residency_windows(closed, method)
    assert first.layout_key == last.layout_key and first.fingerprint != last.fingerprint
    assert len(first.windows) == 2 and len(last.windows) == 8 and last.next_window == 8
    assert [item.key for item in first.windows] == [item.key for item in last.windows[:2]]
    assert first.unplanned_tail_intervals == 1 and last.unplanned_tail_intervals == 0
    tail = plan_residency_windows(closed, method, first_window=last.next_window, limit=1)
    assert tail.next_window is None and tail.windows[0].kind == "final_tail"
    windows = (*last.windows, *tail.windows)
    verified = [app.observed_window(item.original) for item in windows]
    assert sum(item.candidate.observed_microseconds for item in verified) == 135_000_000
    assert all(item.candidate.state == "insufficient_evidence" for item in verified)
    assert all(not item.formal_usage_available for item in windows)
    assert last.platform_owner_id is None and not last.quota_usage_available
    empty = plan_residency_windows(closed, method, first_window=9)
    assert empty.windows == () and empty.next_window is None
    retained = plan_residency_windows(opened, StableResidencyLayoutMethod(max_intervals=4))
    assert retained.windows == () and retained.unplanned_tail_intervals == 3
    assert retained.total_planned_windows == 0
    for arguments in ({"first_window": True}, {"first_window": 10}, {"limit": 9}, {"limit": False}):
        with pytest.raises(ValueError):
            plan_residency_windows(closed, method, **arguments)
    for change in (
        {"formal_usage_available": True},
        {"total_planned_windows": 10},
        {"windows": ()},
        {"witness_fingerprint": "f" * 64},
        {"platform_owner_id": "caller"},
    ):
        with pytest.raises(ValidationError):
            ResidencyLayoutPage.model_validate({**last.model_dump(mode="json"), **change})
    assert counts(state) == before
