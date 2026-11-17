from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.lyra.usage_intervals import UsagePairAssessment, assess_usage_pair
from tests.test_historical_usage_facts import ownership, projection
from tests.test_resource_telemetry import resource_batch

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def frame(
    seconds=0,
    seq=1,
    *,
    uid=1001,
    username="fixture-user",
    count=1,
    quality="ok",
    cache_age=0,
    model="Fixture GPU",
    no_inventory=False,
    shared=False,
):
    payload = resource_batch(
        seq=seq, observed_at=NOW + timedelta(seconds=seconds), compute_age=cache_age
    )
    payload["resources"]["gpu"]["gpus"][0]["model"] = model
    if no_inventory:
        payload["resources"]["gpu"]["gpus"] = []
    compute = payload["resources"]["compute"]
    compute.update(
        quality=quality,
        reported_processes=count if quality == "ok" else None,
        gpus=[{"gpu_uuid": "GPU-a", "reported_processes": count}]
        if quality == "ok" and count
        else [],
    )
    if quality == "ok" and count and uid is not None:
        users = [
            {
                "uid": uid,
                "username": username,
                "reported_processes": count,
                "gpus": [{"gpu_uuid": "GPU-a", "reported_processes": count}],
            }
        ]
        if shared:
            users = [
                {
                    "uid": 1001 + i,
                    "username": "fixture-user-" + str(i),
                    "reported_processes": 1,
                    "gpus": [{"gpu_uuid": "GPU-a", "reported_processes": 1}],
                }
                for i in range(count)
            ]
        ownership(payload, users, quality="ok" if username is not None else "partial")
    return projection(payload)


def pair(a=None, b=None, **limits):
    return assess_usage_pair(
        a or frame(), b or frame(15, 3), max_gap_seconds=limits.get("max_gap_seconds", 45)
    )


def test_stable_linux_owner_is_only_a_sample_interval_candidate_not_platform_usage_charge():
    a, b = frame(), frame(15.25, 3, count=2)
    result = pair(a, b)
    assert result.assessment_scope == "sample_interval_candidate" and result.state == "interval"
    assert result.duration_microseconds == 15250000
    assert (
        result.previous.message_id == a.message_id
        and result.current.fingerprint == b.source_fingerprint
    )
    gpu = result.gpus[0]
    assert gpu.state == "stable_owner" and gpu.uid == 1001 and gpu.username == "fixture-user"
    assert gpu.reason == "SAME_LINUX_OWNER_AT_ENDPOINTS"
    assert all(
        name not in result.model_dump()
        for name in ("gpu_seconds", "user_id", "reservation_id", "ledger_id")
    )


def test_idle_requires_explicit_zero_compute_counts_at_both_endpoints():
    result = pair(frame(count=0), frame(15, 3, count=0))
    assert result.gpus[0].state == "observed_idle" and result.gpus[0].uid is None
    assert result.gpus[0].reason == "NO_COMPUTE_APPS_AT_ENDPOINTS"


def test_exact_source_replay_and_repeated_cached_compute_create_no_new_interval():
    a = frame()
    for result in (
        pair(a, a),
        pair(a, frame(15, 3, cache_age=15)),
        pair(a, frame(15, 3, cache_age=15, model="Changed inventory label")),
    ):
        assert result.state == "same_observation" and result.duration_microseconds is None
        assert result.gpus == ()


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"source_fingerprint": "f" * 64}, "SOURCE_CONTENT_CONFLICT"),
        ({"node_id": "other-node"}, "SOURCE_BINDING_CHANGED"),
        ({"agent_id": "other-agent"}, "SOURCE_BINDING_CHANGED"),
    ],
)
def test_source_conflicts_do_not_become_zero_or_repeated_usage(change, reason):
    a = frame()
    result = pair(a, a.model_copy(update=change))
    assert result.state == "unavailable" and result.reason == reason
    assert result.duration_microseconds is None and result.gpus == ()


@pytest.mark.parametrize(
    "a,b,reason",
    [
        (frame(seq=3), frame(15, 2), "SEQUENCE_NOT_INCREASING"),
        (frame(), frame(15, 3, cache_age=30), "CLOCK_ROLLBACK"),
        (frame(), frame(15, 3, cache_age=15, uid=1002), "OBSERVATION_CHANGED_AT_SAME_TIME"),
        (frame(), frame(46, 3), "OBSERVATION_GAP"),
        (frame(), frame(15, 3, quality="error"), "COMPUTE_UNAVAILABLE"),
    ],
)
def test_gaps_reordering_rollback_changed_cache_and_missing_compute_remain_unavailable(
    a, b, reason
):
    result = pair(a, b)
    assert result.state == "unavailable" and result.reason == reason
    assert result.duration_microseconds is None


def test_continuity_uses_original_compute_time_and_not_interleaved_heartbeat_sequence_counts():
    result = pair(frame(), frame(15, 4))
    assert result.state == "interval" and result.duration_microseconds == 15000000
    assert pair(frame(), frame(45, 10)).state == "interval"
    assert pair(frame(), frame(45.000001, 10)).reason == "OBSERVATION_GAP"


@pytest.mark.parametrize(
    "a,b,reason",
    [
        (frame(), frame(15, 3, uid=1002), "OWNER_CHANGED"),
        (frame(), frame(15, 3, username="renamed-user"), "OWNER_METADATA_CHANGED"),
        (frame(), frame(15, 3, username=None), "OWNERSHIP_INCOMPLETE"),
        (frame(), frame(15, 3, uid=None), "OWNERSHIP_INCOMPLETE"),
        (frame(count=0), frame(15, 3), "USAGE_TRANSITION"),
        (frame(), frame(15, 3, count=0), "USAGE_TRANSITION"),
        (frame(count=2, shared=True), frame(15, 3, count=2, shared=True), "MULTIPLE_OWNERS"),
        (frame(), frame(15, 3, model="Different GPU model"), "GPU_MODEL_CHANGED"),
        (frame(), frame(15, 3, no_inventory=True), "GPU_INVENTORY_CHANGED"),
        (frame(), frame(15, 3, count=0, no_inventory=True), "GPU_INVENTORY_CHANGED"),
    ],
)
def test_ambiguous_per_gpu_intervals_keep_reason_and_no_guessed_owner(a, b, reason):
    result = pair(a, b)
    assert result.state == "interval"
    gpu = result.gpus[0]
    assert gpu.state == "needs_review" and gpu.reason == reason
    assert gpu.uid is None and gpu.username is None


@pytest.mark.parametrize("limit", [True, 0, -1, 3601, "45"])
def test_missing_or_invalid_production_continuity_rule_is_not_silently_defaulted(limit):
    with pytest.raises(DomainError) as error:
        pair(max_gap_seconds=limit)
    assert error.value.code == "VALIDATION_ERROR"


def test_assessment_contract_rejects_fake_zero_duration_and_unavailable_candidates():
    result = pair()
    for update in (
        {"duration_microseconds": 1},
        {"state": "unavailable"},
        {"duration_microseconds": 0},
    ):
        with pytest.raises(ValidationError):
            UsagePairAssessment.model_validate({**result.model_dump(), **update})
    with pytest.raises(ValidationError):
        result.gpus[0].username = "changed"
