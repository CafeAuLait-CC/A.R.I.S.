from itertools import permutations

import pytest

from aris.core.contracts import DomainError
from aris.modules.lyra.usage_trace import MAX_TRACE_MESSAGES, analyze_usage_trace
from tests.test_usage_interval_candidates import frame


def test_reordered_source_messages_reconstruct_the_same_canonical_trace():
    facts = [frame(), frame(15, 3), frame(30, 5)]
    baseline = analyze_usage_trace(facts, max_gap_seconds=45)
    for reordered in permutations(facts):
        assert analyze_usage_trace(reordered, max_gap_seconds=45) == baseline
    assert [a.duration_microseconds for a in baseline.assessments] == [15000000, 15000000]
    assert baseline.first_seq == 1 and baseline.last_seq == 5


def test_exact_message_replays_do_not_duplicate_usage_candidates():
    first, second = frame(), frame(15, 3)
    result = analyze_usage_trace([second, first, first, second], max_gap_seconds=45)
    assert result.unique_messages == 2 and result.duplicate_messages == 2
    assert len(result.assessments) == 1


@pytest.mark.parametrize("kind", ["message_content", "sequence", "source_scope"])
def test_conflicting_or_mixed_sources_are_rejected_without_partial_totals(kind):
    first, second = frame(), frame(15, 3)
    if kind == "message_content":
        second = first.model_copy(update={"source_fingerprint": "f" * 64})
    elif kind == "sequence":
        second = second.model_copy(update={"seq": first.seq})
    else:
        second = second.model_copy(update={"node_id": "other-node"})
    with pytest.raises(DomainError) as error:
        analyze_usage_trace([first, second], max_gap_seconds=45)
    assert error.value.code in {"CONFLICT", "VALIDATION_ERROR"}


def test_cached_frames_do_not_replace_original_inventory_anchor():
    first = frame(model="Old label")
    cached = frame(15, 3, cache_age=15, model="New label")
    fresh = frame(30, 5, model="New label")
    result = analyze_usage_trace([first, cached, fresh], max_gap_seconds=45)
    assert result.assessments[0].state == "same_observation"
    interval = result.assessments[1]
    assert interval.previous.message_id == first.message_id
    assert interval.duration_microseconds == 30000000
    assert (
        interval.gpus[0].state == "needs_review" and interval.gpus[0].reason == "GPU_MODEL_CHANGED"
    )


def test_repeated_cached_envelopes_cannot_hide_a_large_original_compute_gap():
    facts = [frame(), frame(15, 3, cache_age=15), frame(30, 5, cache_age=30), frame(60, 7)]
    result = analyze_usage_trace(facts, max_gap_seconds=45)
    assert [r.state for r in result.assessments] == [
        "same_observation",
        "same_observation",
        "unavailable",
    ]
    assert result.assessments[-1].reason == "OBSERVATION_GAP"


def test_large_gap_restarts_after_the_new_observation_without_bridging_missing_usage():
    facts = [frame(), frame(60, 3), frame(75, 5)]
    result = analyze_usage_trace(facts, max_gap_seconds=45)
    assert result.assessments[0].reason == "OBSERVATION_GAP"
    assert result.assessments[1].state == "interval"
    assert result.assessments[1].previous.message_id == facts[1].message_id


def test_clock_rollback_is_quarantined_until_a_new_point_beyond_the_prior_high_water():
    facts = [
        frame(),
        frame(15, 3),
        frame(30, 5, cache_age=30),
        frame(45, 7, cache_age=40),
        frame(60, 9),
        frame(75, 11),
    ]
    result = analyze_usage_trace(facts, max_gap_seconds=45)
    assert result.assessments[0].state == "interval"
    assert [r.state for r in result.assessments[1:4]] == ["unavailable"] * 3
    assert result.assessments[1].reason == result.assessments[2].reason == "CLOCK_ROLLBACK"
    assert result.assessments[-1].previous.message_id == facts[4].message_id
    assert result.assessments[-1].duration_microseconds == 15000000


def test_changed_same_timestamp_cannot_become_a_valid_owner_anchor_for_the_next_interval():
    facts = [
        frame(),
        frame(15, 3, cache_age=15, uid=1002),
        frame(30, 5, uid=1002),
        frame(45, 7, uid=1002),
    ]
    result = analyze_usage_trace(facts, max_gap_seconds=45)
    assert result.assessments[0].reason == "OBSERVATION_CHANGED_AT_SAME_TIME"
    assert result.assessments[1].state == "unavailable"
    assert result.assessments[2].state == "interval" and result.assessments[2].gpus[0].uid == 1002


def test_unavailable_compute_requires_two_subsequent_good_points_before_new_interval():
    facts = [frame(), frame(15, 3, quality="error"), frame(30, 5), frame(45, 7)]
    result = analyze_usage_trace(facts, max_gap_seconds=45)
    assert [r.reason for r in result.assessments[:2]] == ["COMPUTE_UNAVAILABLE"] * 2
    assert result.assessments[-1].duration_microseconds == 15000000


def test_empty_or_oversized_trace_does_not_claim_zero_or_silently_truncate():
    point = frame()
    for values in ([], [point] * (MAX_TRACE_MESSAGES + 1)):
        with pytest.raises(DomainError):
            analyze_usage_trace(values, max_gap_seconds=45)


def test_a_single_sample_is_retained_without_inventing_elapsed_usage():
    result = analyze_usage_trace([frame()], max_gap_seconds=45)
    assert result.unique_messages == 1 and result.assessments == ()
    assert all(
        key not in result.model_dump() for key in ("gpu_seconds", "ledger_entries", "user_id")
    )
