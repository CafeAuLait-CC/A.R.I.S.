from itertools import permutations

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_sessions import (
    ResidencyFormationRule,
    ResidencySessionAnalysis,
    build_residency_sessions,
    residency_fingerprint,
)
from aris.modules.lyra.trace_state import UsageContinuityRule
from tests.test_usage_interval_candidates import frame

CONTINUITY = UsageContinuityRule(max_gap_seconds=45)


def analyze(values, *, minimum_intervals=2, minimum_duration_seconds=30):
    return build_residency_sessions(
        values,
        continuity=CONTINUITY,
        formation=ResidencyFormationRule(
            minimum_intervals=minimum_intervals, minimum_duration_seconds=minimum_duration_seconds
        ),
    )


def test_adjacent_same_owner_edges_form_one_sampled_residency_candidate():
    values = [frame(), frame(15, 3), frame(30, 5), frame(45, 7)]
    result = analyze(values)
    assert result.candidate_only and not result.formal_usage_available
    assert len(result.sessions) == 1
    run = result.sessions[0]
    assert run.uid == 1001 and run.username == "fixture-user" and run.model == "Fixture GPU"
    assert len(run.evidence) == 3 and run.observed_microseconds == 45000000
    assert run.state == "stable_candidate" and run.end_boundary == "trace_end"
    assert run.evidence[0].previous.message_id == values[0].message_id
    assert run.evidence[-1].current.message_id == values[-1].message_id
    assert all(
        key not in run.model_dump()
        for key in ("user_id", "reservation_id", "ledger_id", "quota_remaining")
    )


def test_multiple_processes_of_one_owner_on_one_gpu_do_not_multiply_gpu_time():
    result = analyze([frame(count=1), frame(15, 3, count=4), frame(30, 5, count=2)])
    assert len(result.sessions) == 1 and result.sessions[0].observed_microseconds == 30000000


def test_source_reorder_and_exact_duplicates_produce_the_same_candidate_segments():
    values = [frame(), frame(15, 3), frame(30, 5)]
    original = analyze(values)
    for reordered in permutations(values):
        replay = analyze(list(reordered) + [values[0]])
        assert (
            replay.sessions == original.sessions
            and replay.rule_fingerprint == original.rule_fingerprint
        )
        assert replay.trace.duplicate_messages == 1


@pytest.mark.parametrize(
    "minimum_intervals,minimum_duration_seconds,state",
    [
        (1, 0, "stable_candidate"),
        (2, 15, "insufficient_evidence"),
        (1, 16, "insufficient_evidence"),
        (1, 15, "stable_candidate"),
    ],
)
def test_short_residency_is_retained_and_threshold_boundaries_are_inclusive(
    minimum_intervals, minimum_duration_seconds, state
):
    result = analyze(
        [frame(), frame(15, 3)],
        minimum_intervals=minimum_intervals,
        minimum_duration_seconds=minimum_duration_seconds,
    )
    assert len(result.sessions) == 1 and result.sessions[0].state == state
    assert result.sessions[0].observed_microseconds == 15000000


def test_cached_compute_does_not_add_an_interval_or_advance_the_source_anchor():
    values = [frame(), frame(15, 3), frame(30, 5, cache_age=15), frame(45, 7)]
    result = analyze(values)
    run = result.sessions[0]
    assert len(run.evidence) == 2 and run.observed_microseconds == 45000000
    assert run.evidence[1].previous.message_id == values[1].message_id
    assert values[2].message_id not in {edge.current.message_id for edge in run.evidence}


def test_owner_change_closes_at_the_last_evidenced_point_and_keeps_transition_unknown():
    values = [frame(), frame(15, 3), frame(30, 5, uid=1002), frame(45, 7, uid=1002)]
    result = analyze(values, minimum_intervals=1, minimum_duration_seconds=0)
    assert len(result.sessions) == 2
    assert [run.uid for run in result.sessions] == [1001, 1002]
    assert [run.observed_microseconds for run in result.sessions] == [15000000] * 2
    assert result.sessions[0].boundary_reason == "OWNER_CHANGED"
    assert result.sessions[0].evidence[-1].current.message_id == values[1].message_id
    assert result.trace.assessments[1].gpus[0].state == "needs_review"


def test_process_disappearance_retains_the_sampled_transition_outside_the_owned_segment():
    result = analyze([frame(), frame(15, 3), frame(30, 5, count=0), frame(45, 7, count=0)])
    assert len(result.sessions) == 1
    assert result.sessions[0].observed_microseconds == 15000000
    assert result.sessions[0].boundary_reason == "USAGE_TRANSITION"
    assert result.trace.assessments[-1].gpus[0].state == "observed_idle"


@pytest.mark.parametrize("kind", ["shared", "unattributed", "idle", "no_inventory"])
def test_unattributable_or_idle_samples_never_create_a_user_residency_candidate(kind):
    kwargs = (
        {"shared": True, "count": 2}
        if kind == "shared"
        else {"uid": None}
        if kind == "unattributed"
        else {"count": 0}
        if kind == "idle"
        else {"no_inventory": True}
    )
    result = analyze([frame(**kwargs), frame(15, 3, **kwargs), frame(30, 5, **kwargs)])
    assert result.sessions == () and not result.formal_usage_available
    expected = "observed_idle" if kind == "idle" else "needs_review"
    assert all(a.gpus[0].state == expected for a in result.trace.assessments)


def test_gaps_and_clock_quarantine_do_not_merge_or_extend_covered_residency():
    gap = analyze([frame(), frame(15, 3), frame(75, 5), frame(90, 7)])
    assert [run.observed_microseconds for run in gap.sessions] == [15000000] * 2
    assert gap.sessions[0].boundary_reason == "OBSERVATION_GAP"
    values = [
        frame(),
        frame(15, 3),
        frame(30, 5, cache_age=30),
        frame(45, 7, cache_age=40),
        frame(60, 9),
        frame(75, 11),
    ]
    rollback = analyze(values)
    assert [run.observed_microseconds for run in rollback.sessions] == [15000000] * 2
    assert rollback.sessions[0].boundary_reason == "CLOCK_ROLLBACK"
    assert rollback.sessions[-1].evidence[0].previous.message_id == values[4].message_id


def test_inventory_label_changes_break_the_run_without_relabeling_older_intervals():
    result = analyze(
        [
            frame(model="Old label"),
            frame(15, 3, model="Old label"),
            frame(30, 5, model="New label"),
            frame(45, 7, model="New label"),
        ]
    )
    assert [run.model for run in result.sessions] == ["Old label", "New label"]
    assert result.sessions[0].boundary_reason == "GPU_MODEL_CHANGED"


def test_distinct_gpu_devices_keep_distinct_residency_evidence():
    values = []
    for i in range(3):
        fact = frame(i * 15, i * 2 + 1)
        another = fact.gpus[0].model_copy(update={"gpu_uuid": "GPU-b", "index": 1})
        values.append(fact.model_copy(update={"gpus": fact.gpus + (another,)}))
    result = analyze(values)
    assert [run.gpu_uuid for run in result.sessions] == ["GPU-a", "GPU-b"]
    assert [run.observed_microseconds for run in result.sessions] == [30000000] * 2


def test_missing_or_single_source_does_not_claim_a_formal_zero_use_total():
    with pytest.raises(DomainError):
        analyze([])
    result = analyze([frame()])
    assert result.sessions == () and not result.formal_usage_available


@pytest.mark.parametrize(
    "kind",
    [
        "missing_evidence",
        "duplicate_evidence",
        "owner",
        "source_fingerprint",
        "duration",
        "threshold",
        "formal_usage",
    ],
)
def test_serialized_candidate_cannot_drop_relabel_duplicate_or_claim_formal_usage(kind):
    result = analyze([frame(), frame(15, 3), frame(30, 5)])
    raw = result.model_dump(mode="json")
    if kind == "missing_evidence":
        raw["sessions"] = []
    elif kind == "duplicate_evidence":
        raw["sessions"].append(raw["sessions"][0])
    elif kind == "owner":
        raw["sessions"][0]["username"] = "another-owner"
    elif kind == "source_fingerprint":
        raw["sessions"][0]["evidence"][0]["previous"]["fingerprint"] = "f" * 64
    elif kind == "duration":
        raw["sessions"][0]["observed_microseconds"] += 1
    elif kind == "threshold":
        raw["sessions"][0]["state"] = "insufficient_evidence"
    else:
        raw["formal_usage_available"] = True
    with pytest.raises(ValidationError):
        ResidencySessionAnalysis.model_validate(raw)


def test_shorter_continuity_rule_cannot_be_applied_to_unrecomputed_long_intervals():
    result = analyze([frame(), frame(15, 3), frame(30, 5)])
    rule = UsageContinuityRule(max_gap_seconds=1)
    raw = result.model_dump(mode="json")
    raw["continuity"] = rule.model_dump(mode="json")
    raw["rule_fingerprint"] = residency_fingerprint(rule, result.formation)
    with pytest.raises(ValidationError):
        ResidencySessionAnalysis.model_validate(raw)


def test_gpu_evidence_size_budget_refuses_oversized_analysis_without_truncation():
    values = []
    for i in range(130):
        fact = frame(i * 15, i * 2 + 1)
        gpus = tuple(
            fact.gpus[0].model_copy(update={"gpu_uuid": "GPU-" + str(k), "index": k})
            for k in range(128)
        )
        values.append(fact.model_copy(update={"gpus": gpus}))
    with pytest.raises(DomainError, match="budget"):
        analyze(values)


@pytest.mark.parametrize(
    "values",
    [
        {"minimum_intervals": 0, "minimum_duration_seconds": 0},
        {"minimum_intervals": True, "minimum_duration_seconds": 0},
        {"minimum_intervals": 1, "minimum_duration_seconds": -1},
        {"minimum_intervals": 1, "minimum_duration_seconds": "30"},
    ],
)
def test_session_formation_thresholds_are_explicit_strict_parameters(values):
    with pytest.raises(ValidationError):
        ResidencyFormationRule.model_validate(values)
