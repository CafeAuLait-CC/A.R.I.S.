from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_segment_service import ResidencySegmentService
from aris.modules.lyra.residency_segments import ResidencySegmentAnalysis, ResidencySegmentQuery
from aris.modules.vega.models import AgentMessage
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_resource_telemetry import put, resource_batch
from tests.test_trace_attestations import attested, incoming, service, snapshot
from tests.test_trace_consumer import CONTEXT, consumer, postgres, rebuild, request, seed

__all__ = ["attested", "consumer"]


def analyzer(state):
    return ResidencySegmentService(
        state.sessions, service(state), state.archive, state.observations, clock=state.now
    )


def query(value):
    return ResidencySegmentQuery(
        attestation_id=value.request.id, expected_attestation_fingerprint=value.fingerprint
    )


def test_short_page_tails_keep_parent_qualification_without_pretending_to_be_whole_usage(attested):
    state, _, attestations = attested
    before = snapshot(state)
    results = [analyzer(state).analyze(query(value.attestation)) for value in attestations]
    assert results[0].segments == ()
    a, b = results[1].segments[0], results[2].segments[0]
    assert a.run_at_segment_end.id == b.run_at_segment_end.id
    assert a.intervals == b.intervals == 1 and a.interval_offset == 0 and b.interval_offset == 1
    assert a.last == b.first and a.observed_microseconds == b.observed_microseconds == 15000000
    assert a.run_state_at_segment_end == "insufficient_evidence"
    assert b.run_state_at_segment_end == "stable_candidate"
    assert all(
        not item.formal_usage_available
        and not item.quota_usage_available
        and item.total_usage_microseconds is None
        and item.time_window_coverage == "not_assessed"
        for item in results
    )
    assert snapshot(state) == before


def test_historical_analysis_fingerprint_is_stable_across_read_times_and_changes_are_rejected(
    attested,
):
    state, _, attestations = attested
    original = analyzer(state).analyze(query(attestations[-1].attestation))
    state.clock["at"] = state.now() + timedelta(seconds=1)
    later = analyzer(state).analyze(original.query)
    assert original.fingerprint == later.fingerprint and later.read_at > original.read_at
    for changes in (
        {"quota_usage_available": True},
        {"formal_usage_available": True},
        {"total_usage_microseconds": 0},
        {"segments": ()},
        {"formation": {"minimum_intervals": 1, "minimum_duration_seconds": 0}},
    ):
        with pytest.raises(ValidationError):
            ResidencySegmentAnalysis.model_validate({**original.model_dump(), **changes})
    for changes in (
        {"owner_id": str(uuid4())},
        {"first_seq": 1},
        {"through_seq": 10},
        {"run_id": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            ResidencySegmentQuery.model_validate({**original.query.model_dump(), **changes})


def test_source_proof_reference_unknown_and_mismatched_fingerprint_are_not_empty_usage(attested):
    state, _, attestations = attested
    original = query(attestations[-1].attestation)
    for changes, code in (
        ({"attestation_id": uuid4()}, "NOT_FOUND"),
        ({"expected_attestation_fingerprint": "f" * 64}, "CONFLICT"),
    ):
        with pytest.raises(DomainError) as error:
            analyzer(state).analyze(original.model_copy(update=changes))
        assert error.value.code == code


@pytest.mark.parametrize(
    "times,ages,expected",
    [((10, 25, 40), (0, 15, 0), 30000000), ((10, 5, 7, 11, 26), None, 15000000)],
)
def test_cached_observations_and_clock_quarantine_never_add_or_bridge_residency_edges(
    consumer, times, ages, expected
):
    state = consumer
    seed(state, times=times, ages=ages)
    segments = []
    for _ in times:
        receipt = state.service.consume(
            CONTEXT, request(state, through_seq=len(times) * 2, limit=2)
        )
        value = service(state).record(CONTEXT, incoming(state, receipt)).attestation
        segments.extend(analyzer(state).analyze(query(value)).segments)
        state.service = rebuild(state)
    assert len(segments) == 1 and segments[0].observed_microseconds == expected
    assert segments[0].intervals == 1


@pytest.mark.parametrize("seq", [3, 5, 6])
def test_current_original_and_cross_page_anchor_corruption_reject_the_entire_analysis(
    attested, seq
):
    state, _, attestations = attested
    with state.sessions.begin() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == seq))
        row.fingerprint = "f" * 64
    before = snapshot(state)
    with pytest.raises(DomainError):
        analyzer(state).analyze(query(attestations[-1].attestation))
    assert snapshot(state) == before


def test_independent_page_partitions_preserve_run_identity_and_exact_disjoint_time_edges(consumer):
    state = consumer
    state.base -= timedelta(hours=1)
    seed(state, times=tuple(10 + 15 * i for i in range(70)))
    signatures = []
    for size in (16, 64):
        config = state.config.model_copy(update={"consumer_id": "segment-partition-" + str(size)})
        app = rebuild(state, config=config)
        values = []
        while app.position().after_seq < 140:
            receipt = app.consume(
                CONTEXT,
                request(
                    state,
                    expected_configuration_fingerprint=config.fingerprint,
                    expected_revision=app.position().revision,
                    through_seq=140,
                    limit=size,
                ),
            )
            proof = service(state).record(CONTEXT, incoming(state, receipt)).attestation
            values.extend(analyzer(state).analyze(query(proof)).segments)
        assert sum(v.intervals for v in values) == 69
        assert sum(v.observed_microseconds for v in values) == 69 * 15000000
        assert all(
            a.last == b.first and a.interval_offset + a.intervals == b.interval_offset
            for a, b in zip(values, values[1:], strict=False)
        )
        signatures.append((values[0].run_at_segment_end.id, values[0].first, values[-1].last))
    assert signatures[0] == signatures[1]


def test_shared_owner_transition_never_bridges_two_local_residency_segments(consumer):
    state = consumer
    for index in range(7):
        users = [owner()]
        if index == 3:
            users.append(owner(uid=1002, username="second-fixture-user"))
        payload = ownership(
            resource_batch(
                seq=index * 2 + 1, observed_at=state.base + timedelta(seconds=10 + 15 * index)
            ),
            users,
        )
        if index == 3:
            payload["resources"]["compute"]["reported_processes"] = 2
            payload["resources"]["compute"]["gpus"][0]["reported_processes"] = 2
        assert put(state.client, payload).status_code == 200
        assert beat(state.client, heartbeat(seq=index * 2 + 2)).status_code == 200
    receipt = state.service.consume(CONTEXT, request(state, through_seq=14))
    proof = service(state).record(CONTEXT, incoming(state, receipt)).attestation
    result = analyzer(state).analyze(query(proof))
    assert len(result.segments) == 2
    a, b = result.segments
    assert a.run_at_segment_end.id != b.run_at_segment_end.id
    assert a.last.compute_observed_at < b.first.compute_observed_at
    assert a.intervals == b.intervals == 2
    assert a.observed_microseconds == b.observed_microseconds == 30000000
    assert result.total_usage_microseconds is None


def test_deadline_and_result_budget_failure_leave_no_storage_side_effects(attested, monkeypatch):
    import aris.modules.lyra.residency_segment_service as module
    import aris.modules.lyra.residency_segments as schemas

    state, _, attestations = attested
    request = query(attestations[-1].attestation)
    before = snapshot(state)
    with monkeypatch.context() as patch:
        ticks = iter((0.0, 20.0))
        patch.setattr(module, "monotonic", lambda: next(ticks))
        with pytest.raises(DomainError) as error:
            analyzer(state).analyze(request)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    with monkeypatch.context() as patch:
        patch.setattr(schemas, "MAX_SEGMENT_ANALYSIS_BYTES", 1)
        with pytest.raises(DomainError):
            analyzer(state).analyze(request)
    assert snapshot(state) == before


def test_postgresql_read_only_analysis_preserves_budgets_and_dirty_orm_work(attested):
    state, _, attestations = attested
    postgres(state)
    request = query(attestations[-1].attestation)
    with state.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        session.execute(text("SET LOCAL lock_timeout='50ms'"))
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 5))
        row.payload = {"pending": "must not become source"}

        def reject(*args):
            raise AssertionError("analysis must not flush caller work")

        event.listen(session, "before_flush", reject)
        try:
            assert len(analyzer(state).analyze_in_transaction(session, request).segments) == 1
            assert tuple(
                session.scalar(text("SHOW " + key))
                for key in ("transaction_timeout", "statement_timeout", "lock_timeout")
            ) == ("2s", "750ms", "50ms")
        finally:
            event.remove(session, "before_flush", reject)


def test_postgresql_locked_source_is_retryable_and_recovers_without_empty_success(attested):
    state, _, attestations = attested
    postgres(state)
    request = query(attestations[-1].attestation)
    with state.sessions.begin() as held:
        held.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            analyzer(state).analyze(request)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert len(analyzer(state).analyze(request).segments) == 1
