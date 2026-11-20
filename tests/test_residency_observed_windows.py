from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_observed_window_schemas import (
    ResidencyObservedWindowReference,
    VerifiedResidencyObservedWindow,
)
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionLimits,
    VerifiedResidencyPartition,
)
from aris.modules.lyra.residency_sessions import residency_fingerprint
from aris.modules.vega.models import AgentMessage
from tests.test_residency_partitions import consumer, partitioned, verifier
from tests.test_residency_segment_store import counts
from tests.test_trace_consumer import postgres

__all__ = ["consumer", "partitioned"]


def observed(state, ref, proof, *, after=1, through=3):
    record = state.service.exact(proof.request.record_id)
    return ResidencyObservedWindowReference(
        reference=ResidencyRunReference(
            record_id=record.request.id,
            expected_record_fingerprint=record.fingerprint,
            run_id=ref.reference.run_id,
            source_cutoff=ref.reference.source_cutoff,
        ),
        attestation_id=proof.request.id,
        expected_attestation_fingerprint=proof.fingerprint,
        expected_trace_configuration_fingerprint=state.config.fingerprint,
        after_interval=after,
        through_interval=through,
    )


def test_fixed_open_and_closed_windows_keep_original_edges_and_separate_qualification(partitioned):
    state, ref, proofs = partitioned
    before = counts(state)
    first = verifier(state).observed_window(observed(state, ref, proofs[0]))
    closed = verifier(state).observed_window(observed(state, ref, proofs[-1]))
    assert first.state_at_record == "open" and closed.state_at_record == "closed"
    assert first.parent_run.intervals == 3 and closed.parent_run.intervals == 9
    assert first.candidate == closed.candidate and first.sources == closed.sources
    assert first.candidate.observed_microseconds == 30_000_000
    assert first.parent_candidate_state == first.candidate.state == "stable_candidate"
    short = verifier(state).observed_window(observed(state, ref, proofs[0], through=2))
    assert short.parent_candidate_state == "stable_candidate"
    assert short.candidate.state == "insufficient_evidence"
    assert short.candidate.observed_microseconds == 15_000_000
    assert short.platform_owner_id is None and not short.formal_usage_available
    assert not short.quota_usage_available and short.time_window_coverage == "not_assessed"
    assert short.parent_run.rule_fingerprint == residency_fingerprint(
        short.continuity, short.formation
    )
    # An observed window cannot be substituted for a closed partition in the writer.
    with pytest.raises(ValidationError):
        VerifiedResidencyPartition.model_validate(first.model_dump(mode="json"))
    assert counts(state) == before


def test_observed_window_requires_exact_fixed_bindings_and_never_accepts_caller_amounts(
    partitioned,
):
    state, ref, proofs = partitioned
    query = observed(state, ref, proofs[0])
    for changes, code in (
        ({"expected_attestation_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_trace_configuration_fingerprint": "f" * 64}, "CONFLICT"),
        ({"through_interval": 4}, "NOT_FOUND"),
        ({"reference": query.reference.model_copy(update={"run_id": "f" * 64})}, "NOT_FOUND"),
        (
            {
                "reference": query.reference.model_copy(
                    update={"source_cutoff": state.now() + timedelta(seconds=1)}
                )
            },
            "VALIDATION_ERROR",
        ),
    ):
        with pytest.raises(DomainError) as error:
            verifier(state).observed_window(query.model_copy(update=changes))
        assert error.value.code == code
    for changes in (
        {"after_interval": True},
        {"after_interval": 3},
        {"through_interval": 130},
        {"owner_id": "caller"},
        {"observed_microseconds": 15_000_000},
    ):
        with pytest.raises(ValidationError):
            ResidencyObservedWindowReference.model_validate({**query.model_dump(), **changes})
    value = verifier(state).observed_window(query)
    for changes in (
        {"formal_usage_available": True},
        {"parent_candidate_state": "insufficient_evidence"},
        {"candidate": value.candidate.model_copy(update={"state": "insufficient_evidence"})},
    ):
        with pytest.raises(ValidationError):
            VerifiedResidencyObservedWindow.model_validate({**value.model_dump(), **changes})


def test_observed_windows_require_current_raw_sources_and_bounded_replay(partitioned):
    state, ref, proofs = partitioned
    query = observed(state, ref, proofs[0])
    before = counts(state)
    with pytest.raises(DomainError):
        verifier(state, limits=ResidencyPartitionLimits(max_source_characters=1)).observed_window(
            query
        )
    assert counts(state) == before
    with state.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 4))
    with pytest.raises(DomainError):
        verifier(state).observed_window(query)
    assert counts(state) == before


def test_postgresql_observed_window_preserves_owner_work_budget_and_recovers_from_locks(
    partitioned,
):
    state, ref, proofs = partitioned
    postgres(state)
    query = observed(state, ref, proofs[0])
    before = counts(state)
    app = verifier(state)
    with state.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL transaction_timeout='2000ms'"))
        db.execute(text("SET LOCAL statement_timeout='750ms'"))
        row = db.scalar(select(AgentMessage).where(AgentMessage.seq == 5))
        row.payload = {"pending": "must not be read or flushed as source"}

        def reject(*args):
            raise AssertionError("observed read must not flush owner work")

        event.listen(db, "before_flush", reject)
        try:
            value = app.observed_window_in_transaction(db, query)
            assert value.state_at_record == "open"
            assert db.scalar(text("SHOW statement_timeout")) == "750ms"
            assert db.scalar(text("SHOW transaction_timeout")) == "2s"
            assert row.payload == {"pending": "must not be read or flushed as source"}
        finally:
            event.remove(db, "before_flush", reject)
    with state.sessions.begin() as blocker:
        blocker.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            app.observed_window(query)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert app.observed_window(query).fingerprint == value.fingerprint
    assert counts(state) == before
