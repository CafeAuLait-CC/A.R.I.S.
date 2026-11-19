import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionLimits,
    ResidencyPartitionMethod,
    ResidencyPartitionReference,
    VerifiedResidencyPartition,
)
from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
from aris.modules.lyra.segment_store_schemas import RecordResidencySegments
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.vega.models import AgentMessage
from tests.test_heartbeat import beat, heartbeat
from tests.test_residency_segment_store import store
from tests.test_residency_segments import query
from tests.test_resource_telemetry import put, resource_batch
from tests.test_trace_attestations import incoming, service, snapshot
from tests.test_trace_consumer import CONTEXT, consumer, postgres, rebuild, request, seed
from tests.test_usage_ledger import POST

__all__ = ["consumer"]
METHOD = ResidencyPartitionMethod(max_intervals=4)


def verifier(state, *, method=METHOD, limits=None):
    return ResidencyPartitionService(
        state.sessions,
        service(state),
        rebuild(state),
        store(state),
        state.archive,
        method=method,
        limits=limits,
        clock=state.now,
    )


def consume_all(state, *, size=8, config=None):
    app = rebuild(state, config=config)
    proofs = []
    while app.position().after_seq < 22:
        receipt = app.consume(
            CONTEXT,
            request(
                state,
                expected_configuration_fingerprint=app.configuration.fingerprint,
                expected_revision=app.position().revision,
                through_seq=22,
                limit=size,
            ),
        )
        proof = service(state).record(CONTEXT, incoming(state, receipt)).attestation
        store(state).record(POST, RecordResidencySegments(id=receipt.id, source=query(proof)))
        proofs.append(proof)
    record = app.exact(receipt.id)
    reference = ResidencyPartitionReference(
        reference=ResidencyRunReference(
            record_id=record.request.id,
            expected_record_fingerprint=record.fingerprint,
            run_id=record.closed_runs[0].run.id,
            source_cutoff=state.now(),
        ),
        closing_attestation_id=proof.request.id,
        expected_closing_attestation_fingerprint=proof.fingerprint,
        expected_trace_configuration_fingerprint=app.configuration.fingerprint,
        expected_method_fingerprint=METHOD.fingerprint,
        partition_index=0,
    )
    return reference, proofs


@pytest.fixture
def partitioned(consumer):
    state = consumer
    seed(state, times=tuple(10 + 15 * i for i in range(10)))
    idle = resource_batch(seq=21, observed_at=state.base + timedelta(seconds=160))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert (
        put(state.client, idle).status_code
        == beat(state.client, heartbeat(seq=22)).status_code
        == 200
    )
    reference, proofs = consume_all(state)
    return state, reference, proofs


def test_balanced_closed_run_windows_preserve_every_edge_and_never_drop_the_short_tail(partitioned):
    state, ref, _ = partitioned
    before = snapshot(state)
    values = [
        verifier(state).verify(ref.model_copy(update={"partition_index": i})) for i in range(3)
    ]
    assert all(
        value.partition_count == 3 and value.through_interval - value.after_interval == 3
        for value in values
    )
    assert [(v.after_interval, v.through_interval) for v in values] == [(0, 3), (3, 6), (6, 9)]
    assert (
        sum(v.candidate.observed_microseconds for v in values)
        == values[0].parent_run.observed_microseconds
        == 135000000
    )
    assert all(
        a.candidate.evidence[-1].current == b.candidate.evidence[0].previous
        for a, b in zip(values, values[1:], strict=False)
    )
    assert all(
        v.candidate.state == "stable_candidate"
        and not v.formal_usage_available
        and not v.quota_usage_available
        for v in values
    )
    assert snapshot(state) == before
    assert verifier(state).verify(ref).fingerprint == values[0].fingerprint


def test_partition_boundaries_do_not_follow_consumption_page_size(partitioned):
    state, ref, _ = partitioned
    alternate, _ = consume_all(
        state,
        size=128,
        config=state.config.model_copy(update={"consumer_id": "alternate-partition-consumer"}),
    )
    for index in range(3):
        a = verifier(state).verify(ref.model_copy(update={"partition_index": index}))
        b = verifier(state).verify(alternate.model_copy(update={"partition_index": index}))
        assert (
            a.parent_run == b.parent_run and a.candidate == b.candidate and a.sources == b.sources
        )


def test_unfit_formation_method_unknown_index_and_binding_cannot_create_a_partition(partitioned):
    state, ref, _ = partitioned
    for changes, code in (
        ({"expected_method_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_trace_configuration_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_closing_attestation_fingerprint": "f" * 64}, "CONFLICT"),
        ({"partition_index": 3}, "NOT_FOUND"),
    ):
        with pytest.raises(DomainError) as error:
            verifier(state).verify(ref.model_copy(update=changes))
        assert error.value.code == code
    method = ResidencyPartitionMethod(max_intervals=1)
    with pytest.raises(DomainError) as error:
        verifier(state, method=method).verify(
            ref.model_copy(update={"expected_method_fingerprint": method.fingerprint})
        )
    assert error.value.code == "USAGE_NEEDS_REVIEW"


def test_open_parent_and_missing_actual_closure_witness_are_not_recordable(partitioned):
    state, ref, proofs = partitioned
    opened = state.service.exact(proofs[0].request.record_id)
    open_ref = ref.model_copy(
        update={
            "reference": ResidencyRunReference(
                record_id=opened.request.id,
                expected_record_fingerprint=opened.fingerprint,
                run_id=ref.reference.run_id,
                source_cutoff=state.now(),
            ),
            "closing_attestation_id": proofs[0].request.id,
            "expected_closing_attestation_fingerprint": proofs[0].fingerprint,
        }
    )
    with pytest.raises(DomainError) as error:
        verifier(state).verify(open_ref)
    assert error.value.code == "USAGE_NEEDS_REVIEW"
    with state.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 21))
    with pytest.raises(DomainError):
        verifier(state).verify(ref)


@pytest.mark.parametrize("kind", ["missing_heartbeat", "rehash_middle", "missing_manifest"])
def test_originals_and_fixed_middle_manifests_are_both_required(partitioned, monkeypatch, kind):
    state, ref, _ = partitioned
    if kind == "missing_manifest":
        monkeypatch.setattr(UsageTraceRepository, "source_records", lambda *args, **kwargs: ())
    else:
        with state.sessions.begin() as session:
            if kind == "missing_heartbeat":
                session.execute(delete(AgentMessage).where(AgentMessage.seq == 4))
            else:
                row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 5))
                payload = dict(row.payload)
                payload = json.loads(json.dumps(payload))
                payload["resources"]["system"]["cpu"]["utilization_percent"] = 41
                row.payload = payload
                row.fingerprint = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
    before = snapshot(state)
    with pytest.raises(DomainError):
        verifier(state).verify(ref)
    assert snapshot(state) == before


def test_later_partition_revalidates_its_bounded_range_without_claiming_all_previous_raw_history(
    partitioned,
):
    state, ref, _ = partitioned
    with state.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 4))
    assert verifier(state).verify(ref.model_copy(update={"partition_index": 2})).after_interval == 6
    with pytest.raises(DomainError):
        verifier(state).verify(ref)


def test_source_record_character_and_elapsed_budgets_refuse_without_returning_partial_evidence(
    partitioned, monkeypatch
):
    import aris.modules.lyra.residency_partition_service as module

    state, ref, _ = partitioned
    before = snapshot(state)
    middle = ref.model_copy(update={"partition_index": 1})
    for limits in (
        ResidencyPartitionLimits(max_source_messages=2),
        ResidencyPartitionLimits(max_manifest_records=1),
        ResidencyPartitionLimits(max_source_characters=1),
    ):
        with pytest.raises(DomainError):
            verifier(state, limits=limits).verify(middle)
    with monkeypatch.context() as patch:
        ticks = iter((0.0, 20.0))
        patch.setattr(module, "monotonic", lambda: next(ticks))
        with pytest.raises(DomainError) as error:
            verifier(state).verify(ref)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert snapshot(state) == before


def test_fixed_source_cutoff_and_query_authority_cannot_be_replaced_by_callers(partitioned):
    state, ref, _ = partitioned
    future = ref.model_copy(
        update={
            "reference": ref.reference.model_copy(
                update={"source_cutoff": state.now() + timedelta(seconds=1)}
            )
        }
    )
    with pytest.raises(DomainError) as error:
        verifier(state).verify(future)
    assert error.value.code == "VALIDATION_ERROR"
    value = verifier(state).verify(ref)
    for change in (
        {"owner_id": "caller"},
        {"start_at": state.now()},
        {"observed_microseconds": 0},
        {"partition_index": True},
    ):
        with pytest.raises(ValidationError):
            ResidencyPartitionReference.model_validate({**ref.model_dump(), **change})
    for change in (
        {"quota_usage_available": True},
        {"formal_usage_available": True},
        {"sources": ()},
        {"through_interval": 99},
    ):
        with pytest.raises(ValidationError):
            VerifiedResidencyPartition.model_validate({**value.model_dump(), **change})


def test_postgresql_read_only_partition_preserves_owner_budget_and_pending_orm(partitioned):
    state, ref, _ = partitioned
    postgres(state)
    with state.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 5))
        row.payload = {"pending": "must not replace original"}

        def reject(*args):
            raise AssertionError("partition read must not flush owner work")

        event.listen(session, "before_flush", reject)
        try:
            assert verifier(state).verify_in_transaction(session, ref).partition_count == 3
            assert (
                session.scalar(text("SHOW transaction_timeout")) == "2s"
                and session.scalar(text("SHOW statement_timeout")) == "750ms"
            )
        finally:
            event.remove(session, "before_flush", reject)


def test_postgresql_locked_original_source_is_retryable_without_a_partial_partition(partitioned):
    state, ref, _ = partitioned
    postgres(state)
    with state.sessions.begin() as held:
        held.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            verifier(state).verify(ref)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert verifier(state).verify(ref).partition_count == 3
