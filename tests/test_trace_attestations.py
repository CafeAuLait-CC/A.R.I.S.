import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.lyra.models import UsageTraceAttestationRow
from aris.modules.lyra.trace_attestation_schemas import (
    StoredTraceAttestation,
    TraceAttestationRequest,
)
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    StoredTraceConsumption,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.vega.models import AgentMessage
from tests.test_heartbeat import beat, heartbeat
from tests.test_residency_evidence import reference
from tests.test_resource_telemetry import put, resource_batch
from tests.test_trace_consumer import CONTEXT, consumer, counts, postgres, rebuild, request, seed

__all__ = ["consumer"]


def service(state):
    return TraceAttestationService(state.sessions, rebuild(state), clock=state.now)


def incoming(state, receipt):
    record = state.service.exact(receipt.id)
    return TraceAttestationRequest(
        id=uuid4(),
        record_id=receipt.id,
        expected_record_fingerprint=record.fingerprint,
        source_cutoff=state.now(),
    )


def snapshot(state):
    with state.sessions() as session:
        extra = session.scalar(select(func.count()).select_from(UsageTraceAttestationRow))
    return (*counts(state), extra)


@pytest.fixture
def attested(consumer):
    state = consumer
    seed(state)
    receipts, attestations = [], []
    for _ in range(3):
        receipt = state.service.consume(CONTEXT, request(state, limit=2))
        attestations.append(service(state).record(CONTEXT, incoming(state, receipt)))
        receipts.append(receipt)
        state.service = rebuild(state)
    return state, receipts, attestations


def test_bounded_original_proofs_survive_restart_without_claiming_complete_time_coverage(attested):
    state, receipts, attestations = attested
    before = snapshot(state)
    for index, result in enumerate(attestations):
        value = result.attestation
        assert service(state).exact(value.request.id) == value
        assert service(state).reverify(value.request.id) == value
        replay = service(state).record(CONTEXT, value.request)
        assert replay.duplicate and replay.attestation == value
        assert value.revision == index + 1 and value.source_count == 2
        assert value.telemetry_count == 1
        assert value.previous_id == (
            attestations[index - 1].attestation.request.id if index else None
        )
        assert not value.formal_usage_available and not value.quota_usage_available
        assert value.time_window_coverage == "not_assessed"
        assert "fixture-user" not in value.model_dump_json()
        assert state.service.exact(receipts[index].id).closed_runs == ()
        with state.sessions() as session:
            public = session.get(DomainEvent, str(value.event_id))
            assert "fixture-user" not in json.dumps(public.payload)
    assert snapshot(state) == before


def test_missing_direct_predecessor_blocks_skipped_segments_but_same_input_can_retry(consumer):
    state = consumer
    seed(state)
    first = state.service.consume(CONTEXT, request(state, limit=2))
    second = state.service.consume(CONTEXT, request(state))
    value, before = incoming(state, second), snapshot(state)
    with pytest.raises(DomainError) as error:
        service(state).record(CONTEXT, value)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and snapshot(state) == before
    original = service(state).record(CONTEXT, incoming(state, first))
    result = service(state).record(CONTEXT, value)
    assert result.attestation.previous_id == original.attestation.request.id
    assert result.attestation.first_seq == 3 and not result.duplicate


def test_fixed_request_fingerprint_cutoff_and_single_proof_per_source_record(attested):
    state, receipts, attestations = attested
    value = attestations[0].attestation
    before = snapshot(state)
    for change in (
        {"expected_record_fingerprint": "f" * 64},
        {"record_id": receipts[-1].id},
        {"source_cutoff": value.request.source_cutoff + timedelta(microseconds=1)},
        {"id": uuid4()},
    ):
        with pytest.raises(DomainError) as error:
            service(state).record(CONTEXT, value.request.model_copy(update=change))
        assert error.value.code == "CONFLICT"
    assert snapshot(state) == before


def test_future_and_pre_record_cutoffs_do_not_append_a_proof(consumer):
    state = consumer
    seed(state)
    result = state.service.consume(CONTEXT, request(state))
    value, before = incoming(state, result), snapshot(state)
    record = state.service.exact(result.id)
    for cutoff, expected in (
        (state.now() + timedelta(hours=1), "VALIDATION_ERROR"),
        (record.created_at - timedelta(microseconds=1), "CONFLICT"),
    ):
        with pytest.raises(DomainError) as error:
            service(state).record(CONTEXT, value.model_copy(update={"source_cutoff": cutoff}))
        assert error.value.code == expected and snapshot(state) == before


@pytest.mark.parametrize("corruption", ["missing", "heartbeat", "anchor", "cutoff"])
def test_current_segment_and_old_anchor_are_verified_against_originals(attested, corruption):
    state, _, attestations = attested
    value = attestations[-1].attestation
    with state.sessions.begin() as session:
        if corruption == "missing":
            session.execute(delete(AgentMessage).where(AgentMessage.seq == 6))
        else:
            seq = {"heartbeat": 6, "anchor": 3, "cutoff": 5}[corruption]
            row = session.scalar(select(AgentMessage).where(AgentMessage.seq == seq))
            if corruption == "cutoff":
                row.received_at = state.now() + timedelta(hours=1)
            else:
                row.fingerprint = "f" * 64
    before = snapshot(state)
    assert service(state).exact(value.request.id) == value  # Historical receipt still exists.
    with pytest.raises(DomainError):
        service(state).reverify(value.request.id)
    assert snapshot(state) == before


def test_revalidating_a_later_segment_does_not_claim_rechecking_all_prior_originals(attested):
    state, _, attestations = attested
    # Seq 2 is neither in the third segment nor its compute anchor. Its loss
    # invalidates the first proof's current recheck, not a historical receipt.
    with state.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 2))
    assert service(state).reverify(attestations[-1].attestation.request.id)
    with pytest.raises(DomainError):
        service(state).reverify(attestations[0].attestation.request.id)
    assert not attestations[-1].attestation.quota_usage_available


def test_rehashed_wrong_transition_state_cannot_be_attested(consumer, monkeypatch):
    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    original = state.service.exact(receipt.id)
    altered = original.model_copy(update={"transitions_fingerprint": "f" * 64})
    forged = StoredTraceConsumption.model_validate(
        {
            **altered.model_dump(mode="json"),
            "fingerprint": trace_fingerprint(
                "record", canonical_trace(altered, exclude={"fingerprint"})
            ),
        }
    )
    get = UsageTraceRepository.get
    monkeypatch.setattr(
        UsageTraceRepository, "get", lambda repo, id: forged if id == receipt.id else get(repo, id)
    )
    value, before = incoming(state, receipt), snapshot(state)
    with pytest.raises(DomainError) as error:
        service(state).record(CONTEXT, value)
    assert error.value.code == "CONFLICT" and snapshot(state) == before


@pytest.mark.parametrize("times,ages", [((10, 25, 40), (0, 15, 0)), ((10, 5, 7, 11, 26), None)])
def test_cached_anchor_and_clock_rollback_semantics_survive_segment_revalidation(
    consumer, times, ages
):
    state = consumer
    seed(state, times=times, ages=ages)
    values = []
    for _ in times:
        receipt = state.service.consume(
            CONTEXT, request(state, through_seq=len(times) * 2, limit=2)
        )
        values.append(service(state).record(CONTEXT, incoming(state, receipt)).attestation)
        state.service = rebuild(state)
    assert all(service(state).reverify(item.request.id) == item for item in values)
    run = receipt.position.residency.open_runs[0]
    assert run.intervals == 1
    assert run.observed_microseconds == (30000000 if ages else 15000000)


def test_deadline_failure_does_not_commit_a_provisional_segment_and_same_key_recovers(
    consumer, monkeypatch
):
    import aris.modules.lyra.trace_attestation_service as module

    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    value, before = incoming(state, receipt), snapshot(state)
    with monkeypatch.context() as patch:
        ticks = iter((0.0, 20.0))
        patch.setattr(module, "monotonic", lambda: next(ticks))
        with pytest.raises(DomainError) as error:
            service(state).record(CONTEXT, value)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert snapshot(state) == before
    assert not service(state).record(CONTEXT, value).duplicate


@pytest.mark.parametrize("when", ["before", "after"])
def test_unknown_commit_recovers_the_original_attestation_without_duplicate_effects(
    consumer, monkeypatch, when
):
    import aris.modules.lyra.trace_attestation_service as module

    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    value = incoming(state, receipt)
    original = module.commit_owned_transaction

    def interrupted(session):
        if when == "after":
            original(session)
        raise RuntimeError("fixture unknown source attestation acknowledgement")

    monkeypatch.setattr(module, "commit_owned_transaction", interrupted)
    with pytest.raises(RuntimeError):
        service(state).record(CONTEXT, value)
    monkeypatch.setattr(module, "commit_owned_transaction", original)
    result = service(state).record(CONTEXT, value)
    assert result.duplicate is (when == "after") and snapshot(state)[-1] == 1


def test_final_original_recheck_failure_and_outer_rollback_leave_no_attestation(
    consumer, monkeypatch
):
    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    value, before = incoming(state, receipt), snapshot(state)
    verifier = rebuild(state)
    app = TraceAttestationService(state.sessions, verifier, clock=state.now)
    original, calls = verifier.verify_record_in_transaction, []

    def changed(*args):
        calls.append(True)
        if len(calls) == 2:
            raise DomainError("CONFLICT", "fixture source changed after provisional proof", 409)
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(verifier, "verify_record_in_transaction", changed)
        with pytest.raises(DomainError):
            app.record(CONTEXT, value)
    assert snapshot(state) == before
    with state.sessions() as session:
        assert app.record_in_transaction(session, CONTEXT, value)
        session.rollback()
    assert snapshot(state) == before
    assert not app.record(CONTEXT, value).duplicate


@pytest.mark.parametrize("table", ["domain_events", "usage_trace_attestations", "event_outbox"])
def test_partial_write_failure_rolls_back_proof_and_event_history(consumer, table):
    state = consumer
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    value, before = incoming(state, receipt), snapshot(state)

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO " + table):
            raise RuntimeError("fixture interrupted attestation write")

    engine = state.client.app.state.engine
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            service(state).record(CONTEXT, value)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert snapshot(state) == before
    assert not service(state).record(CONTEXT, value).duplicate


def test_empty_migration_roundtrip_and_immutable_nonempty_history(consumer):
    state = consumer
    seed(state)
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0019")
    command.upgrade(cfg, "head")
    command.check(cfg)
    receipt = state.service.consume(CONTEXT, request(state))
    value = service(state).record(CONTEXT, incoming(state, receipt)).attestation
    for statement in (
        "DELETE FROM usage_trace_attestations",
        "UPDATE usage_trace_attestations SET fingerprint='changed'",
    ):
        with pytest.raises(DBAPIError), state.sessions.begin() as session:
            session.execute(text(statement))
    with pytest.raises(RuntimeError, match="source attestation history prevents downgrade"):
        command.downgrade(cfg, "0019")
    assert service(state).reverify(value.request.id) == value


def test_contract_cannot_supply_owner_checkpoint_amount_or_full_coverage(attested):
    state, _, attestations = attested
    value = attestations[-1].attestation
    for changes in (
        {"owner_id": str(uuid4())},
        {"checkpoint": {}},
        {"observed_microseconds": 1},
        {"complete": True},
    ):
        with pytest.raises(ValidationError):
            TraceAttestationRequest.model_validate({**value.request.model_dump(), **changes})
    for changes in (
        {"formal_usage_available": True},
        {"quota_usage_available": True},
        {"time_window_coverage": "complete"},
        {"previous_id": None},
    ):
        with pytest.raises(ValidationError):
            StoredTraceAttestation.model_validate({**value.model_dump(), **changes})


def test_postgresql_read_only_recheck_preserves_owner_budgets_and_dirty_work(attested):
    state, _, attestations = attested
    postgres(state)
    value = attestations[-1].attestation
    with state.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        session.execute(text("SET LOCAL lock_timeout='50ms'"))
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 5))
        row.payload = {"pending": "must not affect originals"}

        def reject(*args):
            raise AssertionError("source recheck must not flush caller work")

        event.listen(session, "before_flush", reject)
        try:
            assert service(state).reverify_in_transaction(session, value.request.id) == value
            assert tuple(
                session.scalar(text("SHOW " + key))
                for key in ("transaction_timeout", "statement_timeout", "lock_timeout")
            ) == ("2s", "750ms", "50ms")
        finally:
            event.remove(session, "before_flush", reject)


@pytest.mark.parametrize("same_key", [True, False])
def test_postgresql_competing_proofs_append_one_committed_segment(consumer, same_key):
    state = consumer
    postgres(state)
    seed(state)
    receipt = state.service.consume(CONTEXT, request(state))
    value = incoming(state, receipt)
    other = value if same_key else value.model_copy(update={"id": uuid4()})
    gate = Barrier(2)

    def run(incoming):
        gate.wait(timeout=5)
        try:
            return service(state).record(CONTEXT, incoming)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (value, other)))
    assert snapshot(state)[-1] == 1
    if same_key:
        assert sorted(result.duplicate for result in results) == [False, True]
    else:
        assert results.count("CONFLICT") == 1


def test_postgresql_uncommitted_predecessor_is_not_used_as_a_committed_proof(consumer):
    state = consumer
    postgres(state)
    seed(state)
    first = state.service.consume(CONTEXT, request(state, limit=2))
    second = state.service.consume(CONTEXT, request(state))
    first_request, second_request = incoming(state, first), incoming(state, second)
    with state.sessions() as writer:
        service(state).record_in_transaction(writer, CONTEXT, first_request)
        # A read-only recheck doesn't acquire the writer's consumer gate.
        with state.sessions() as reader:
            record = state.service.exact(second.id)
            with pytest.raises(DomainError) as error:
                service(state)._previous(reader, record, state.now())
            assert error.value.code == "DEPENDENCY_UNAVAILABLE"
        writer.commit()
    assert service(state).record(CONTEXT, second_request).attestation.revision == 2
    with pytest.raises(DBAPIError), state.sessions.begin() as session:
        session.execute(text("TRUNCATE usage_trace_attestations CASCADE"))


def test_postgresql_locked_original_source_is_retryable_and_then_recovers(attested):
    state, _, attestations = attested
    postgres(state)
    value = attestations[-1].attestation
    with state.sessions.begin() as held:
        held.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            service(state).reverify(value.request.id)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert service(state).reverify(value.request.id) == value


def test_long_residency_pipeline_preserves_open_state_then_records_every_closed_partition(consumer):
    from pydantic import SecretStr

    from aris.core.access.admin_schemas import (
        AdministrationBatch,
        AdministrationContext,
        AssignNodeUser,
        BootstrapRequest,
        DirectoryProfile,
        IdentityNamespace,
    )
    from aris.core.access.administration import AccessAdministrationService
    from aris.core.access.history_service import NodeUserHistoryService
    from aris.core.access.schemas import ExternalSubject
    from aris.modules.lyra.derivation_service import UsageDerivationService
    from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration, UsagePostingContext
    from aris.modules.lyra.ledger_service import UsageLedgerService
    from aris.modules.lyra.partition_recording_service import PartitionRecordingService
    from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
    from aris.modules.lyra.recording_job_service import RecordingJobService
    from aris.modules.lyra.recording_plan_schemas import (
        RecordingDispatchRequest,
        RecordingPlanRequest,
    )
    from aris.modules.lyra.recording_plan_service import RecordingPlanService
    from aris.modules.lyra.reservation_links import ResidencyReservationService
    from aris.modules.lyra.residency_attribution import ResidencyAttributionService
    from aris.modules.lyra.residency_evidence_service import ResidencyRunEvidenceService
    from aris.modules.lyra.residency_partition_schemas import (
        ResidencyPartitionMethod,
        ResidencyPartitionReference,
    )
    from aris.modules.lyra.residency_partition_service import ResidencyPartitionService
    from aris.modules.lyra.residency_segment_service import ResidencySegmentService
    from aris.modules.lyra.residency_segments import ResidencySegmentQuery
    from aris.modules.lyra.residency_sessions import residency_fingerprint
    from aris.modules.lyra.run_recording_repo import ClosedRunRecordingRepository
    from aris.modules.lyra.run_recording_service import ClosedRunRecordingService
    from aris.modules.lyra.segment_store_schemas import RecordResidencySegments
    from aris.modules.lyra.segment_store_service import ResidencySegmentStore
    from aris.modules.nova.usage_link_service import ReservationUsageLinkService
    from aris.modules.vega.scope_service import VegaNodeScopes

    state = consumer
    state.base -= timedelta(hours=8)
    points = 1030
    seed(state, times=tuple(10 + 15 * i for i in range(points)))
    # Explicit synthetic identity history covers this synthetic stream. It is
    # independent of any real Slack member, permission or runtime configuration.
    key = SecretStr("isolated-long-pipeline-operator-key-123456789")
    admin = AccessAdministrationService(
        state.sessions,
        IdentityNamespace(provider="slack", namespace="TLONGPIPELINE"),
        operator_key=key,
        permission_catalog=(),
        scopes=VegaNodeScopes(state.sessions),
        clock=lambda: state.base,
    )
    actor = admin.bootstrap(
        BootstrapRequest(
            subject=ExternalSubject(
                provider="slack", namespace="TLONGPIPELINE", external_id="ULONGFIXTURE"
            ),
            profile=DirectoryProfile(display_name="Long pipeline fixture", timezone="UTC"),
            operator_proof=key,
            request_id="long-pipeline-bootstrap",
        )
    ).user_id
    mapping = admin.apply(
        AdministrationContext(
            actor_id=actor,
            request_id="long-pipeline-map",
            reason="Explicit isolated fixture mapping",
        ),
        AdministrationBatch(
            changes=(AssignNodeUser(node_id="node-a", os_username="fixture-user", user_id=actor),)
        ),
    )
    posting_context = UsagePostingContext(request_id="fixture-long-source-segments")
    saved_segments = ResidencySegmentStore(
        state.sessions,
        ResidencySegmentService(
            state.sessions, service(state), state.archive, state.observations, clock=state.now
        ),
        clock=state.now,
    )
    parent, first_run, final, max_queries = None, None, None, 0
    local_segments = []
    for _ in range((points * 2 + 127) // 128):
        final = state.service.consume(CONTEXT, request(state, through_seq=points * 2))
        statements = []

        def capture(connection, cursor, statement, parameters, context, many, target=statements):
            target.append(statement)

        engine = state.client.app.state.engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            value = service(state).record(CONTEXT, incoming(state, final)).attestation
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert value.previous_id == parent and value.source_count <= 128
        parent = value.request.id
        local_segments.extend(
            saved_segments.record(
                posting_context,
                RecordResidencySegments(
                    id=uuid4(),
                    source=ResidencySegmentQuery(
                        attestation_id=value.request.id,
                        expected_attestation_fingerprint=value.fingerprint,
                    ),
                ),
            ).value.analysis.segments
        )
        max_queries = max(max_queries, len(statements))
        run = final.position.residency.open_runs[0]
        first_run = first_run or run.id
        assert run.id == first_run and final.closed_run_count == 0
        state.service = rebuild(state)
    assert run.intervals == points - 1 and run.observed_microseconds == (points - 1) * 15000000
    assert snapshot(state)[-1] == 17 and max_queries <= 35
    assert sum(item.intervals for item in local_segments) == points - 1
    assert sum(item.observed_microseconds for item in local_segments) == run.observed_microseconds
    assert all(
        a.last == b.first and a.interval_offset + a.intervals == b.interval_offset
        for a, b in zip(local_segments, local_segments[1:], strict=False)
    )
    # The old whole-window read limit remains intact; no cap was silently raised.
    from aris.modules.lyra.residency_evidence_schemas import ResidencyVerificationLimits

    with pytest.raises(DomainError) as error:
        ResidencyRunEvidenceService(
            state.sessions,
            state.archive,
            limits=ResidencyVerificationLimits(max_source_messages=1024),
            clock=state.now,
        ).verify(reference(state, final))
    assert error.value.code == "USAGE_EVIDENCE_TOO_LARGE"
    idle = resource_batch(
        seq=points * 2 + 1, observed_at=state.base + timedelta(seconds=10 + 15 * points)
    )
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(state.client, idle).status_code == 200
    assert beat(state.client, heartbeat(seq=points * 2 + 2)).status_code == 200
    closed = state.service.consume(CONTEXT, request(state, through_seq=points * 2 + 2))
    end = service(state).record(CONTEXT, incoming(state, closed)).attestation
    assert end.previous_id == parent and closed.closed_run_count == 1
    assert state.service.exact(closed.id).closed_runs[0].run == run
    method = ResidencyPartitionMethod(max_intervals=128)
    partition_service = ResidencyPartitionService(
        state.sessions,
        service(state),
        rebuild(state),
        saved_segments,
        state.archive,
        method=method,
        clock=state.now,
    )
    parent_reference = ResidencyPartitionReference(
        reference=reference(state, closed),
        closing_attestation_id=end.request.id,
        expected_closing_attestation_fingerprint=end.fingerprint,
        expected_trace_configuration_fingerprint=state.config.fingerprint,
        expected_method_fingerprint=method.fingerprint,
        partition_index=0,
    )
    number = (run.intervals + method.max_intervals - 1) // method.max_intervals
    identities = NodeUserHistoryService(state.sessions, clock=state.now)
    derivations = UsageDerivationService(
        state.sessions,
        state.observations,
        ResidencyAttributionService(state.sessions, identities),
        ResidencyReservationService(
            state.sessions, ReservationUsageLinkService(state.sessions, clock=state.now)
        ),
        clock=state.now,
    )
    ledger = UsageLedgerService(
        state.sessions,
        derivations,
        configuration=UsagePostingConfiguration(
            source_ref="isolated-long-pipeline-recording",
            recording_start_at=state.base,
            timing_fingerprint=residency_fingerprint(
                state.config.continuity, state.config.formation
            ),
        ),
        clock=state.now,
    )
    writer = PartitionRecordingService(
        state.sessions, partition_service, identities, derivations, ledger, clock=state.now
    )
    whole = ClosedRunRecordingService(
        state.sessions,
        ResidencyRunEvidenceService(state.sessions, state.archive, clock=state.now),
        state.archive,
        derivations,
        ledger,
        clock=state.now,
    )
    jobs = RecordingJobService(
        state.sessions,
        whole,
        partition_recorder=writer,
        limits=RecordingJobLimits(
            transaction_timeout_ms=30000, lease_seconds=60, max_attempts=3, retry_base_seconds=1
        ),
        clock=state.now,
    )
    plans = RecordingPlanService(state.sessions, partition_service, ledger, jobs, clock=state.now)
    plan = plans.create(
        posting_context,
        RecordingPlanRequest(
            id=uuid4(),
            partition=parent_reference,
            expected_recording_configuration_fingerprint=ledger.configuration.fingerprint,
            identity_revision=mapping.revision,
        ),
    ).plan
    first_batch = plans.dispatch(
        posting_context,
        RecordingDispatchRequest(plan_id=plan.request.id, expected_version=1, limit=8),
    )
    assert first_batch.progress.next_partition == 8 and not first_batch.progress.all_enqueued
    # A new service reconstructs dispatch progress; no in-memory fanout cursor.
    plans = RecordingPlanService(state.sessions, partition_service, ledger, jobs, clock=state.now)
    end_batch = plans.dispatch(
        posting_context,
        RecordingDispatchRequest(plan_id=plan.request.id, expected_version=2, limit=8),
    )
    assert len(end_batch.job_ids) == 1 and end_batch.progress.all_enqueued
    for _ in range(number):
        claim = jobs.claim(posting_context)
        assert jobs.execute(posting_context, claim).state == "completed"
    receipts = [writer.record(posting_context, plan.job(index)) for index in range(number)]
    assert all(receipt.duplicate for receipt in receipts)
    with state.sessions() as session:
        partitions = [
            ClosedRunRecordingRepository(session).get_partition(receipt.id).proof
            for receipt in receipts
        ]
    assert (
        number == 9 and sum(len(value.candidate.evidence) for value in partitions) == run.intervals
    )
    assert (
        sum(value.candidate.observed_microseconds for value in partitions)
        == run.observed_microseconds
    )
    assert all(
        a.candidate.evidence[-1].current == b.candidate.evidence[0].previous
        for a, b in zip(partitions, partitions[1:], strict=False)
    )
    assert all(len(value.sources) <= 1024 and value.source_count <= 8192 for value in partitions)
    entries = [ledger.exact(receipt.ledger.entries[0]) for receipt in receipts]
    assert (
        sum(entry.details.measurement.observed_microseconds for entry in entries)
        == run.observed_microseconds
    )
    assert all(entry.details.measurement.owner_id == actor for entry in entries)
    assert counts(state)[-1] == number and not end.quota_usage_available
