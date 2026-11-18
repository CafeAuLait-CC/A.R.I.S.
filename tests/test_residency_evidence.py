from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_evidence_schemas import (
    ResidencyRunReference,
    ResidencyVerificationLimits,
    VerifiedResidencyWindow,
)
from aris.modules.lyra.residency_evidence_service import ResidencyRunEvidenceService
from aris.modules.lyra.residency_sessions import ResidencyEvidence
from aris.modules.lyra.residency_state import ResidencyRun, _digest
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    StoredTraceConsumption,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.vega.models import AgentMessage
from tests.test_heartbeat import beat, heartbeat
from tests.test_resource_telemetry import put, resource_batch
from tests.test_trace_consumer import CONTEXT, consumer, counts, postgres, rebuild, request, seed

__all__ = ["consumer"]


def reference(state, receipt):
    record = state.service.exact(receipt.id)
    run = (
        record.closed_runs[0].run if record.closed_runs else receipt.position.residency.open_runs[0]
    )
    return ResidencyRunReference(
        record_id=record.request.id,
        expected_record_fingerprint=record.fingerprint,
        run_id=run.id,
        source_cutoff=state.now(),
    )


@pytest.fixture
def evidence(consumer):
    state = consumer
    seed(state)
    for _ in range(3):
        receipt = state.service.consume(CONTEXT, request(state, limit=2))
        state.service = rebuild(state)
    verifier = ResidencyRunEvidenceService(state.sessions, state.archive, clock=state.now)
    return state, verifier, reference(state, receipt)


def test_original_manifest_and_archives_reproduce_a_fixed_run_without_granting_usage_authority(
    evidence,
):
    state, verifier, ref = evidence
    before = counts(state)
    first = verifier.verify(ref)
    second = verifier.verify(ref)
    assert first.run.observed_microseconds == 30000000 and first.run.intervals == 2
    assert first.source_count == 6 and first.telemetry_count == 3 and first.manifest_records == 3
    assert first.candidate_state == "stable_candidate" and first.state_at_record == "open"
    assert first.fingerprint == second.fingerprint and first.read_at <= second.read_at
    assert first.platform_owner_id is None and first.total_usage_microseconds is None
    assert not first.formal_usage_available and not first.quota_usage_available
    assert counts(state) == before


def test_closed_candidate_and_historical_open_reference_have_independent_fixed_evidence(evidence):
    state, verifier, ref = evidence
    before = verifier.verify(ref)
    idle = resource_batch(seq=7, observed_at=state.base + timedelta(seconds=55))
    idle["resources"]["compute"].update(reported_processes=0, gpus=[])
    assert put(state.client, idle).status_code == 200
    assert beat(state.client, heartbeat(seq=8)).status_code == 200
    receipt = state.service.consume(CONTEXT, request(state, through_seq=8))
    closed = verifier.verify(reference(state, receipt))
    assert closed.state_at_record == "closed" and closed.run == before.run
    assert verifier.verify(ref).fingerprint == before.fingerprint
    closed_reference = reference(state, receipt)
    with state.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 7))
    with pytest.raises(DomainError) as error:
        verifier.verify(closed_reference)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    assert verifier.verify(ref).fingerprint == before.fingerprint


def test_reference_fingerprint_run_membership_and_cutoff_are_enforced(evidence):
    state, verifier, ref = evidence
    created = state.service.exact(ref.record_id).created_at
    for changes, code in (
        ({"expected_record_fingerprint": "f" * 64}, "CONFLICT"),
        ({"run_id": "f" * 64}, "NOT_FOUND"),
        ({"source_cutoff": created - timedelta(microseconds=1)}, "DEPENDENCY_UNAVAILABLE"),
        ({"source_cutoff": state.now() + timedelta(hours=1)}, "VALIDATION_ERROR"),
    ):
        with pytest.raises(DomainError) as error:
            verifier.verify(ref.model_copy(update=changes))
        assert error.value.code == code


@pytest.mark.parametrize("corruption", ["missing_packet", "heartbeat_hash", "receive_time"])
def test_missing_or_changed_originals_never_become_a_partially_verified_amount(
    evidence, corruption
):
    state, verifier, ref = evidence
    with state.sessions.begin() as session:
        if corruption == "missing_packet":
            session.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
        else:
            row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 2))
            if corruption == "heartbeat_hash":
                row.fingerprint = "f" * 64
            else:
                row.received_at -= timedelta(seconds=1)
    before = counts(state)
    with pytest.raises(DomainError) as error:
        verifier.verify(ref)
    assert error.value.code == (
        "CONFLICT" if corruption == "receive_time" else "DEPENDENCY_UNAVAILABLE"
    )
    assert counts(state) == before


def test_missing_saved_manifest_cannot_be_replaced_by_raw_archive_presence(evidence, monkeypatch):
    state, verifier, ref = evidence
    before = counts(state)
    monkeypatch.setattr(UsageTraceRepository, "source_records", lambda *args, **kwargs: ())
    with pytest.raises(DomainError) as error:
        verifier.verify(ref)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE" and counts(state) == before


def test_rehashed_but_incorrect_run_summary_is_rejected_by_actual_source_replay(
    evidence, monkeypatch
):
    state, verifier, ref = evidence
    original = state.service.exact(ref.record_id)
    run = original.after_residency.open_runs[0]
    # A valid one-edge contract over the same endpoints omits the actual middle
    # observation. Its new hashes cannot make that false interval count true.
    wrong = ResidencyRun.model_validate(
        {
            **run.model_dump(),
            "intervals": 1,
            "evidence_fingerprint": _digest(
                "first_evidence",
                {
                    "id": run.id,
                    "edge": ResidencyEvidence(previous=run.first, current=run.last).model_dump(
                        mode="json"
                    ),
                },
            ),
        }
    )
    altered = original.model_copy(
        update={
            "after_residency": original.after_residency.model_copy(update={"open_runs": (wrong,)})
        }
    )
    forged = StoredTraceConsumption.model_validate(
        {
            **altered.model_dump(mode="json"),
            "fingerprint": trace_fingerprint(
                "record", canonical_trace(altered, exclude={"fingerprint"})
            ),
        }
    )
    get = UsageTraceRepository.get
    records = UsageTraceRepository.source_records
    monkeypatch.setattr(
        UsageTraceRepository,
        "get",
        lambda repo, id: forged if id == ref.record_id else get(repo, id),
    )

    def altered_manifest(repo, *args, **kwargs):
        return tuple(
            forged if row.request.id == ref.record_id else row
            for row in records(repo, *args, **kwargs)
        )

    monkeypatch.setattr(UsageTraceRepository, "source_records", altered_manifest)
    with pytest.raises(DomainError) as error:
        verifier.verify(ref.model_copy(update={"expected_record_fingerprint": forged.fingerprint}))
    assert error.value.code == "CONFLICT"


def test_source_record_and_time_budgets_fail_without_returning_a_proof(evidence, monkeypatch):
    state, verifier, ref = evidence
    before = counts(state)
    for limits in (
        ResidencyVerificationLimits(max_source_messages=2),
        ResidencyVerificationLimits(max_records=1),
    ):
        bounded = ResidencyRunEvidenceService(
            state.sessions, state.archive, limits=limits, clock=state.now
        )
        with pytest.raises(DomainError) as error:
            bounded.verify(ref)
        assert error.value.code == "USAGE_EVIDENCE_TOO_LARGE"
    ticks = iter((0.0, 20.0))
    with monkeypatch.context() as patch:
        patch.setattr("aris.modules.lyra.residency_evidence_service.monotonic", lambda: next(ticks))
        with pytest.raises(DomainError) as error:
            verifier.verify(ref)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert counts(state) == before and verifier.verify(ref).run.intervals == 2


def test_source_verification_ignores_dirty_orm_without_flushing_caller_work(evidence):
    state, verifier, ref = evidence
    original = verifier.verify(ref)
    with state.sessions() as session:
        row = session.scalar(select(AgentMessage).where(AgentMessage.seq == 1))
        row.payload = {"pending": "must not replace original facts"}

        def reject(*args):
            raise AssertionError("source proof must not flush owner state")

        event.listen(session, "before_flush", reject)
        try:
            proof = verifier.verify_in_transaction(session, ref)
            assert proof.fingerprint == original.fingerprint
        finally:
            event.remove(session, "before_flush", reject)


def test_multiple_archive_and_manifest_pages_have_a_bounded_query_count(consumer):
    state = consumer
    state.base -= timedelta(hours=1)
    seed(state, times=tuple(10 + 15 * index for index in range(70)))
    for _ in range(9):
        receipt = state.service.consume(CONTEXT, request(state, through_seq=140, limit=16))
        state.service = rebuild(state)
    verifier = ResidencyRunEvidenceService(state.sessions, state.archive, clock=state.now)
    ref = reference(state, receipt)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = state.client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        proof = verifier.verify(ref)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert proof.source_count == 140 and proof.telemetry_count == 70 and proof.manifest_records == 9
    assert proof.run.intervals == 69 and proof.run.observed_microseconds == 1035000000
    assert len(statements) <= 11
    assert all(statement.startswith("SELECT") for statement in statements)


def test_proof_schema_and_reference_cannot_override_owner_amount_or_complete_usage(evidence):
    _, verifier, ref = evidence
    for changes in ({"owner_id": str(uuid4())}, {"observed_microseconds": 0}, {"complete": True}):
        with pytest.raises(ValidationError):
            ResidencyRunReference.model_validate({**ref.model_dump(), **changes})
    proof = verifier.verify(ref)
    for changes in (
        {"formal_usage_available": True},
        {"quota_usage_available": True},
        {"platform_owner_id": str(uuid4())},
        {"total_usage_microseconds": 0},
        {"source_count": 0},
    ):
        with pytest.raises(ValidationError):
            VerifiedResidencyWindow.model_validate({**proof.model_dump(), **changes})


def test_postgresql_read_only_proof_preserves_the_owners_transaction_budget(evidence):
    state, verifier, ref = evidence
    postgres(state)
    with state.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        session.execute(text("SET LOCAL lock_timeout='50ms'"))

        def settings():
            return tuple(
                session.scalar(text("SHOW " + name))
                for name in ("transaction_timeout", "statement_timeout", "lock_timeout")
            )

        before = settings()
        proof = verifier.verify_in_transaction(session, ref)
        assert proof.run.observed_microseconds == 30000000
        assert settings() == before


def test_postgresql_source_lock_timeout_is_retryable_without_partial_proof(evidence):
    state, verifier, ref = evidence
    postgres(state)
    with state.sessions.begin() as locked:
        locked.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            verifier.verify(ref)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert verifier.verify(ref).run.observed_microseconds == 30000000
