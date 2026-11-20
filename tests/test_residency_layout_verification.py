from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.residency_layout_verification_schemas import StableResidencyWindowReference
from aris.modules.lyra.residency_layout_verification_service import StableResidencyWindowService
from aris.modules.lyra.residency_partition_schemas import VerifiedResidencyPartition
from aris.modules.vega.models import AgentMessage
from tests.test_residency_observed_windows import consumer, observed, partitioned
from tests.test_residency_partitions import verifier
from tests.test_residency_segment_store import counts

__all__ = ["consumer", "partitioned"]


def reference(query, method, *, index=0):
    return StableResidencyWindowReference(
        **query.model_dump(exclude={"after_interval", "through_interval"}),
        expected_method_fingerprint=method.fingerprint,
        window_index=index,
    )


def service(state, *, capacity=4, originals=None):
    return StableResidencyWindowService(
        state.sessions,
        originals or verifier(state),
        method=StableResidencyLayoutMethod(max_intervals=capacity),
        clock=state.now,
    )


def test_server_selects_only_stable_layout_windows_and_preserves_independent_qualification(
    partitioned,
):
    state, ref, proofs = partitioned
    before = counts(state)
    app = service(state)
    query = reference(observed(state, ref, proofs[-1]), app.method)
    values = [app.verify(query.model_copy(update={"window_index": index})) for index in range(3)]
    assert [
        (v.original.request.after_interval, v.original.request.through_interval) for v in values
    ] == [(0, 4), (4, 6), (6, 9)]
    assert [v.window.kind for v in values] == ["stable_prefix", "final_tail", "final_tail"]
    assert sum(v.original.candidate.observed_microseconds for v in values) == 135_000_000
    assert app.verify(query).fingerprint == values[0].fingerprint
    # The parent is stable, but one interval is still insufficient for independent formation.
    short_reader = service(state, capacity=1)
    short_query = reference(observed(state, ref, proofs[0]), short_reader.method)
    short = short_reader.verify(short_query)
    assert short.original.parent_candidate_state == "stable_candidate"
    assert short.original.candidate.state == "insufficient_evidence"
    assert not short.formal_usage_available and not short.quota_usage_available
    assert short.platform_owner_id is None and short.time_window_coverage == "not_assessed"
    with pytest.raises(ValidationError):
        VerifiedResidencyPartition.model_validate(short.model_dump(mode="json"))
    for added in (
        {"after_interval": 0},
        {"owner_id": "caller"},
        {"observed_microseconds": 10},
        {"window_index": True},
    ):
        with pytest.raises(ValidationError):
            StableResidencyWindowReference.model_validate({**query.model_dump(), **added})
    assert counts(state) == before


def test_method_source_scope_budget_and_retained_tail_cannot_be_bypassed(partitioned, monkeypatch):
    state, ref, proofs = partitioned
    app = service(state)
    query = reference(observed(state, ref, proofs[-1]), app.method)
    for changes, code in (
        ({"expected_method_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_attestation_fingerprint": "f" * 64}, "CONFLICT"),
        ({"expected_trace_configuration_fingerprint": "f" * 64}, "CONFLICT"),
        ({"window_index": 3}, "NOT_FOUND"),
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
            app.verify(query.model_copy(update=changes))
        assert error.value.code == code
    with pytest.raises(DomainError) as error:
        app.verify(reference(observed(state, ref, proofs[0]), app.method))
    assert error.value.code == "NOT_FOUND"  # This fixed prefix has no eligible full window yet.

    class ChangingReader:
        def observed_window_in_transaction(self, session, request):
            result = verifier(state).observed_window_in_transaction(session, request)
            app.method = StableResidencyLayoutMethod(max_intervals=3)
            return result

    app = service(state, originals=ChangingReader())
    with pytest.raises(DomainError) as error:
        app.verify(query)
    assert error.value.code == "CONFLICT"
    app = service(state)
    with monkeypatch.context() as scoped:
        times = iter((0.0, 0.0, 20.0))
        scoped.setattr(
            "aris.modules.lyra.residency_layout_verification_service.monotonic", lambda: next(times)
        )
        with pytest.raises(DomainError) as error:
            app.verify(query)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    # Removing a current original source must not produce a fabricated valid plan proof.
    with state.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 2))
    with pytest.raises(DomainError):
        app.verify(query)
