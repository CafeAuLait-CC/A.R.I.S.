import json
import random
from datetime import timedelta

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.lyra.trace_state import (
    MAX_CHECKPOINT_BYTES,
    UsageContinuityRule,
    UsageTraceCheckpoint,
    UsageTraceStep,
    advance_usage_trace,
    decode_trace_checkpoint,
    encode_trace_checkpoint,
    observation_stamp,
)
from aris.modules.lyra.usage_trace import analyze_usage_trace
from tests.test_usage_interval_candidates import frame

RULE = UsageContinuityRule(max_gap_seconds=45)


def stream(facts):
    checkpoint = None
    sources = {}
    steps = []
    for fact in facts:
        anchor = sources.get(checkpoint.anchor.reference.message_id) if checkpoint else None
        result = advance_usage_trace(checkpoint, fact, rule=RULE, anchor=anchor)
        steps.append(result)
        sources[fact.message_id] = fact
        checkpoint = decode_trace_checkpoint(encode_trace_checkpoint(result.checkpoint))
    return checkpoint, steps


@pytest.mark.parametrize(
    "kind",
    ["normal", "cached", "gap", "rollback", "same_time_changed", "compute_error", "owner_change"],
)
def test_serialized_restart_after_every_point_matches_complete_offline_trace(kind):
    values = [frame(), frame(15, 3), frame(30, 5), frame(45, 7)]
    if kind == "cached":
        values = [
            frame(model="Old label"),
            frame(15, 3, cache_age=15, model="New label"),
            frame(30, 5, model="New label"),
        ]
    elif kind == "gap":
        values = [frame(), frame(60, 3), frame(75, 5)]
    elif kind == "rollback":
        values = [
            frame(),
            frame(15, 3),
            frame(30, 5, cache_age=30),
            frame(45, 7, cache_age=40),
            frame(60, 9),
            frame(75, 11),
        ]
    elif kind == "same_time_changed":
        values = [
            frame(),
            frame(15, 3, cache_age=15, uid=1002),
            frame(30, 5, uid=1002),
            frame(45, 7, uid=1002),
        ]
    elif kind == "compute_error":
        values[1] = frame(15, 3, quality="error")
    elif kind == "owner_change":
        values[1:] = [frame(15 * i, 2 * i + 1, uid=1002) for i in range(1, 4)]
    checkpoint, steps = stream(values)
    expected = analyze_usage_trace(values, max_gap_seconds=45)
    assert tuple(step.assessment for step in steps if step.assessment) == expected.assessments
    assert checkpoint.last.reference.message_id == values[-1].message_id
    assert all(step.candidate_only for step in steps)


def test_many_state_transitions_preserve_all_offline_quarantine_and_cache_decisions():
    randomizer = random.Random(9467)
    values = []
    seconds = 0
    for index in range(120):
        seconds += randomizer.choice([5, 15, 15, 60])
        values.append(
            frame(
                seconds,
                index * 2 + 1,
                uid=randomizer.choice([1001, 1002, None]),
                cache_age=randomizer.choice([0, 0, 15, 60]),
            )
        )
    _, steps = stream(values)
    expected = analyze_usage_trace(values, max_gap_seconds=45)
    assert tuple(step.assessment for step in steps[1:]) == expected.assessments


def test_replayed_last_and_original_cached_anchor_never_produce_an_extra_assessment():
    original = frame()
    cached = frame(15, 3, cache_age=15)
    checkpoint, _ = stream([original, cached])
    for known in (cached, original):
        result = advance_usage_trace(checkpoint, known, rule=RULE)
        assert result.kind == "replayed" and result.assessment is None
        assert result.checkpoint == checkpoint and not result.requires_rebuild


def test_unrecognized_older_source_requires_reconstruction_without_advancing_the_cursor():
    values = [frame(), frame(15, 3), frame(30, 7)]
    checkpoint, _ = stream(values)
    late = frame(20, 5)
    result = advance_usage_trace(checkpoint, late, rule=RULE)
    assert result.kind == "late_requires_rebuild" and result.requires_rebuild
    assert result.incoming.message_id == late.message_id
    assert result.checkpoint == checkpoint and result.assessment is None


@pytest.mark.parametrize(
    "kind",
    ["message_payload", "message_fingerprint", "same_sequence", "anchor_sequence", "binding"],
)
def test_known_source_conflicts_or_mixed_bindings_do_not_change_a_checkpoint(kind):
    a, b = frame(), frame(15, 3, cache_age=15)
    checkpoint, _ = stream([a, b])
    if kind == "message_payload":
        bad = b.model_copy(
            update={"gpus": a.gpus[::-1], "received_at": b.received_at + timedelta(seconds=1)}
        )
    elif kind == "message_fingerprint":
        bad = b.model_copy(update={"source_fingerprint": "f" * 64})
    elif kind == "same_sequence":
        bad = frame(15, 3)
    elif kind == "anchor_sequence":
        bad = frame(seq=1)
    else:
        bad = frame(30, 5).model_copy(update={"node_id": "another-source"})
    with pytest.raises(DomainError) as error:
        advance_usage_trace(checkpoint, bad, rule=RULE)
    assert error.value.code == ("VALIDATION_ERROR" if kind == "binding" else "CONFLICT")


def test_a_new_point_requires_the_original_verified_anchor_not_an_updated_cached_copy():
    a, cached = frame(), frame(15, 3, cache_age=15)
    checkpoint, _ = stream([a, cached])
    with pytest.raises(DomainError) as missing:
        advance_usage_trace(checkpoint, frame(30, 5), rule=RULE)
    assert missing.value.code == "DEPENDENCY_UNAVAILABLE"
    for changed in (
        cached,
        a.model_copy(update={"received_at": a.received_at + timedelta(seconds=1)}),
    ):
        with pytest.raises(DomainError) as conflict:
            advance_usage_trace(checkpoint, frame(30, 5), rule=RULE, anchor=changed)
        assert conflict.value.code == "CONFLICT"


def test_a_rule_change_requires_reconstruction_instead_of_reusing_the_old_high_water():
    a = frame()
    checkpoint, _ = stream([a])
    with pytest.raises(DomainError) as error:
        advance_usage_trace(
            checkpoint, frame(15, 3), anchor=a, rule=UsageContinuityRule(max_gap_seconds=60)
        )
    assert error.value.code == "CONFLICT"


def test_a_clock_quarantine_survives_restart_and_the_first_recovery_does_not_bridge_it():
    first, high, back = frame(), frame(15, 3), frame(30, 5, cache_age=30)
    checkpoint, _ = stream([first, high, back])
    assert checkpoint.blocked_at == high.compute_observed_at
    checkpoint = decode_trace_checkpoint(encode_trace_checkpoint(checkpoint))
    recovery = frame(60, 7)
    recovered = advance_usage_trace(checkpoint, recovery, rule=RULE, anchor=high)
    assert (
        recovered.assessment.state == "unavailable"
        and recovered.assessment.duration_microseconds is None
    )
    assert recovered.checkpoint.blocked_at is None
    next_step = advance_usage_trace(recovered.checkpoint, frame(75, 9), rule=RULE, anchor=recovery)
    assert next_step.assessment.duration_microseconds == 15000000


def test_checkpoint_storage_keeps_source_references_and_digests_without_copying_owner_facts():
    point = frame(username="PRIVATE_FIXTURE_OWNER")
    checkpoint, _ = stream([point])
    body = encode_trace_checkpoint(checkpoint)
    assert len(body) <= MAX_CHECKPOINT_BYTES
    assert (
        b"PRIVATE_FIXTURE_OWNER" not in body and b'"gpus"' not in body and b'"owners"' not in body
    )
    assert decode_trace_checkpoint(body) == checkpoint
    assert not any(
        key in checkpoint.model_dump() for key in ("user_id", "gpu_seconds", "ledger_id")
    )


@pytest.mark.parametrize(
    "kind", ["empty", "oversized", "duplicate", "nonfinite", "utf8", "deep", "unknown"]
)
def test_invalid_checkpoint_encoding_fails_with_a_bounded_generic_error(kind):
    checkpoint, _ = stream([frame()])
    body = encode_trace_checkpoint(checkpoint)
    if kind == "empty":
        body = b""
    elif kind == "oversized":
        body = b"x" * (MAX_CHECKPOINT_BYTES + 1)
    elif kind == "duplicate":
        body = body.replace(
            b'"candidate_only":true', b'"candidate_only":false,"candidate_only":true', 1
        )
    elif kind == "nonfinite":
        body = body.replace(b'"max_gap_seconds":45', b'"max_gap_seconds":NaN')
    elif kind == "utf8":
        body = b"\xff"
    elif kind == "deep":
        body = b"[" * 1200 + b"0" + b"]" * 1200
    else:
        raw = json.loads(body)
        raw["PRIVATE_FIXTURE_FIELD"] = True
        body = json.dumps(raw).encode()
    with pytest.raises(DomainError) as error:
        decode_trace_checkpoint(body)
    assert error.value.code == "VALIDATION_ERROR" and "PRIVATE_FIXTURE_FIELD" not in str(
        error.value
    )


@pytest.mark.parametrize(
    "kind",
    [
        "rule_fingerprint",
        "source_scope",
        "anchor_sequence",
        "compute_high_water",
        "blocked_high_water",
    ],
)
def test_checkpoint_invariants_reject_unchecked_or_corrupted_state(kind):
    a, b = frame(), frame(15, 3)
    checkpoint, _ = stream([a, b])
    raw = checkpoint.model_dump(mode="json")
    if kind == "rule_fingerprint":
        raw["rule_fingerprint"] = "0" * 64
    elif kind == "source_scope":
        raw["anchor"]["reference"]["node_id"] = "another-node"
    elif kind == "anchor_sequence":
        raw["anchor"]["reference"]["seq"] += 1
    elif kind == "compute_high_water":
        raw["anchor"]["reference"]["compute_observed_at"] = a.compute_observed_at.isoformat()
    else:
        raw["blocked_at"] = a.compute_observed_at.isoformat()
    with pytest.raises(ValidationError):
        UsageTraceCheckpoint.model_validate(raw)
    bad = checkpoint.model_copy(update={"rule_fingerprint": "f" * 64})
    with pytest.raises(DomainError):
        advance_usage_trace(bad, frame(30, 5), rule=RULE, anchor=b)


@pytest.mark.parametrize("value", [True, 0, -1, 3601, "45"])
def test_continuity_limit_is_explicit_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        UsageContinuityRule(max_gap_seconds=value)


def test_initialization_and_step_flags_cannot_claim_unsupported_progress():
    point = frame()
    with pytest.raises(DomainError):
        advance_usage_trace(None, point, rule=RULE, anchor=point)
    result = advance_usage_trace(None, point, rule=RULE)
    with pytest.raises(ValidationError):
        UsageTraceStep.model_validate({**result.model_dump(), "requires_rebuild": True})
    with pytest.raises(ValidationError):
        UsageTraceStep.model_validate({**result.model_dump(), "kind": "advanced"})
    with pytest.raises(ValidationError):
        observation_stamp(point.model_copy(update={"seq": 2**63}))
