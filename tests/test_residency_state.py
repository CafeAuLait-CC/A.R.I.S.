import random

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_sessions import ResidencyFormationRule, build_residency_sessions
from aris.modules.lyra.residency_state import (
    ResidencyFormationCheckpoint,
    advance_residency_formation,
    preview_open_residencies,
)
from aris.modules.lyra.trace_state import (
    UsageContinuityRule,
    advance_usage_trace,
    decode_trace_checkpoint,
    encode_trace_checkpoint,
)
from tests.test_usage_interval_candidates import frame

CONTINUITY = UsageContinuityRule(max_gap_seconds=45)
FORMATION = ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30)


def walk(values, *, formation=FORMATION, restart_every=1):
    trace, residency = None, None
    sources, closed, steps = {}, [], []
    for index, fact in enumerate(values, start=1):
        anchor = sources.get(trace.anchor.reference.message_id) if trace else None
        step = advance_usage_trace(trace, fact, rule=CONTINUITY, anchor=anchor)
        result = advance_residency_formation(
            residency, step, continuity=CONTINUITY, formation=formation
        )
        residency, trace = result.checkpoint, step.checkpoint
        closed.extend(result.closed)
        steps.append(step)
        sources[fact.message_id] = fact
        if index % restart_every == 0:
            trace = decode_trace_checkpoint(encode_trace_checkpoint(trace))
            residency = ResidencyFormationCheckpoint.model_validate_json(
                residency.model_dump_json()
            )
    return residency, tuple(closed), trace, steps


def normalize_closed(values):
    return sorted(
        [
            (
                item.run.gpu_uuid,
                item.run.uid,
                item.run.username,
                item.run.model,
                item.run.first,
                item.run.last,
                item.run.intervals,
                item.run.observed_microseconds,
                item.state,
                item.end_boundary,
                item.boundary_reason,
            )
            for item in values
        ],
        key=lambda value: (value[4].seq, value[0], value[5].seq),
    )


def normalize_whole(values):
    return sorted(
        [
            (
                item.gpu_uuid,
                item.uid,
                item.username,
                item.model,
                item.evidence[0].previous,
                item.evidence[-1].current,
                len(item.evidence),
                item.observed_microseconds,
                item.state,
                item.end_boundary,
                item.boundary_reason,
            )
            for item in values
        ],
        key=lambda value: (value[4].seq, value[0], value[5].seq),
    )


@pytest.mark.parametrize(
    "kind",
    [
        "normal",
        "cached",
        "gap",
        "rollback",
        "idle",
        "owner",
        "model",
        "incomplete",
        "shared",
        "inventory",
        "username",
        "compute_error",
    ],
)
def test_restarted_incremental_candidates_match_whole_trace_with_all_quality_boundaries(kind):
    values = [frame(), frame(15, 3), frame(30, 5), frame(45, 7)]
    if kind == "cached":
        values[2] = frame(30, 5, cache_age=15)
    elif kind == "gap":
        values[2:] = [frame(90, 5), frame(105, 7)]
    elif kind == "rollback":
        values = [
            frame(),
            frame(15, 3),
            frame(30, 5, cache_age=30),
            frame(45, 7, cache_age=40),
            frame(60, 9),
            frame(75, 11),
        ]
    elif kind == "idle":
        values[2:] = [frame(30, 5, count=0), frame(45, 7, count=0)]
    elif kind == "owner":
        values[2:] = [frame(30, 5, uid=1002), frame(45, 7, uid=1002)]
    elif kind == "model":
        values[2:] = [frame(30, 5, model="Changed GPU"), frame(45, 7, model="Changed GPU")]
    elif kind == "incomplete":
        values = [frame(15 * i, i * 2 + 1, uid=None) for i in range(4)]
    elif kind == "shared":
        values = [frame(15 * i, i * 2 + 1, count=2, shared=True) for i in range(4)]
    elif kind == "inventory":
        values[2] = frame(30, 5, no_inventory=True)
    elif kind == "username":
        values[2:] = [frame(30, 5, username="Changed user"), frame(45, 7, username="Changed user")]
    elif kind == "compute_error":
        values[2] = frame(30, 5, quality="error")
    state, closed, _, _ = walk(values)
    whole = build_residency_sessions(values, continuity=CONTINUITY, formation=FORMATION)
    assert normalize_closed(closed + preview_open_residencies(state)) == normalize_whole(
        whole.sessions
    )
    assert state.candidate_only and not state.formal_usage_available


def test_page_size_serialization_and_restart_do_not_change_run_identity_or_evidence_chain():
    values = [frame(i * 15, 2 * i + 1, count=1 + i % 4) for i in range(20)]
    first = walk(values, restart_every=1)
    for size in (2, 5, 50):
        state, closed, _, _ = walk(values, restart_every=size)
        assert state == first[0] and closed == first[1] == ()
    run = first[0].open_runs[0]
    assert run.intervals == 19 and run.observed_microseconds == 19 * 15 * 1000000
    assert len(preview_open_residencies(first[0])) == 1
    assert first[0].open_runs[0] == run


@pytest.mark.parametrize(
    "minimum_intervals,seconds,expected",
    [
        (1, 0, "stable_candidate"),
        (2, 15, "insufficient_evidence"),
        (1, 16, "insufficient_evidence"),
        (1, 15, "stable_candidate"),
    ],
)
def test_short_open_preview_retains_exact_formation_threshold_semantics(
    minimum_intervals, seconds, expected
):
    rule = ResidencyFormationRule(
        minimum_intervals=minimum_intervals, minimum_duration_seconds=seconds
    )
    state, _, _, _ = walk([frame(), frame(15, 3)], formation=rule)
    assert preview_open_residencies(state)[0].state == expected


def test_many_restarts_preserve_quality_transitions_and_do_not_merge_distinct_gpus():
    randomizer = random.Random(6121)
    values, seconds = [], 0
    for index in range(120):
        seconds += randomizer.choice((5, 15, 15, 60))
        item = frame(
            seconds,
            2 * index + 1,
            uid=randomizer.choice((1001, 1002, None)),
            cache_age=randomizer.choice((0, 0, 15)),
        )
        other = item.gpus[0].model_copy(update={"gpu_uuid": "GPU-b", "index": 1})
        values.append(item.model_copy(update={"gpus": item.gpus + (other,)}))
    state, closed, _, _ = walk(values)
    whole = build_residency_sessions(values, continuity=CONTINUITY, formation=FORMATION)
    assert normalize_closed(closed + preview_open_residencies(state)) == normalize_whole(
        whole.sessions
    )


def test_long_residency_keeps_bounded_checkpoint_size_instead_of_all_original_edges():
    values = [frame(i * 15, 2 * i + 1) for i in range(1100)]
    early = walk(values[:20])[0]
    state, closed, _, _ = walk(values)
    assert closed == () and len(state.open_runs) == 1
    assert state.open_runs[0].intervals == 1099
    assert state.open_runs[0].observed_microseconds == 1099 * 15 * 1000000
    assert len(state.model_dump_json()) < len(early.model_dump_json()) + 128
    assert "evidence" not in state.open_runs[0].model_dump()


def test_exact_replay_never_closes_or_extends_the_open_run():
    values = [frame(), frame(15, 3), frame(30, 5, cache_age=15)]
    state, closed, trace, _ = walk(values)
    for fact in (values[1], values[2]):
        step = advance_usage_trace(trace, fact, rule=CONTINUITY)
        replay = advance_residency_formation(
            state, step, continuity=CONTINUITY, formation=FORMATION
        )
        assert replay.checkpoint == state and replay.closed == closed == ()


def test_missing_state_changed_rules_or_late_sources_require_reconstruction():
    values = [frame(), frame(15, 3), frame(30, 7)]
    state, _, trace, steps = walk(values)
    with pytest.raises(DomainError) as error:
        advance_residency_formation(None, steps[-1], continuity=CONTINUITY, formation=FORMATION)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    changed = ResidencyFormationRule(minimum_intervals=1, minimum_duration_seconds=0)
    with pytest.raises(DomainError):
        advance_residency_formation(state, steps[-1], continuity=CONTINUITY, formation=changed)
    late = advance_usage_trace(trace, frame(20, 5), rule=CONTINUITY)
    with pytest.raises(DomainError) as error:
        advance_residency_formation(state, late, continuity=CONTINUITY, formation=FORMATION)
    assert error.value.code == "CONFLICT"


def test_open_state_cannot_change_identity_span_or_progress_without_invalidating_its_contract():
    state = walk([frame(), frame(15, 3)])[0]
    original = state.open_runs[0]
    for change in (
        {"id": "f" * 64},
        {"observed_microseconds": 1},
        {"uid": 1002},
        {"intervals": 3},
        {"evidence_fingerprint": "f" * 64},
    ):
        raw = state.model_dump()
        raw["open_runs"] = ({**original.model_dump(), **change},)
        with pytest.raises(ValidationError):
            ResidencyFormationCheckpoint.model_validate(raw)
    with pytest.raises(ValidationError):
        ResidencyFormationCheckpoint.model_validate(
            {**state.model_dump(), "open_runs": (original, original)}
        )
