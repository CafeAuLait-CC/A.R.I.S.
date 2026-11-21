from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete

from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_schemas import UsagePostingRequest
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.stable_window_recording_service import StableWindowRecordingService
from aris.modules.vega.models import AgentMessage
from tests.test_stable_window_acceptance import (
    acceptance,
    admin_state,
    counts,
    derivations,
    history_state,
    ledger,
    open_reference,
    partition_recording,
)
from tests.test_usage_ledger import POST

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
]


@pytest.fixture
def stable_recording(acceptance):
    app, request, fixture = acceptance
    method = StableResidencyLayoutMethod(max_intervals=3)
    app.evidence.method = method
    request = request.model_copy(
        update={
            "window": request.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    writer = fixture[0]
    service = StableWindowRecordingService(writer.sessions, app, writer.derivations, writer.ledger)
    return service, request, acceptance


def test_open_prefix_closure_and_tail_record_exact_time_and_replay_history_after_retention(
    stable_recording,
):
    service, closed, (app, _, _) = stable_recording
    opened = open_reference(app, closed)
    first = service.accept_and_record(POST, opened)
    assert first.acceptance.first_request == opened and first.ledger.effect == "recorded"
    repeated = service.accept_and_record(POST, closed)
    assert repeated.duplicate and repeated.model_copy(update={"duplicate": False}) == first
    receipts = [first] + [
        service.accept_and_record(
            POST,
            closed.model_copy(
                update={"window": closed.window.model_copy(update={"window_index": index})}
            ),
        )
        for index in (1, 2)
    ]
    entries = [service.ledger.exact(value.ledger.entries[0]) for value in receipts]
    windows = [entry.details.measurement for entry in entries]
    assert sum(window.observed_microseconds for window in windows) == 135000000
    assert all(
        left.end_at == right.start_at for left, right in zip(windows, windows[1:], strict=False)
    )
    assert all(
        not value.quota_usage_available and value.time_window_coverage == "not_assessed"
        for value in receipts
    )
    assert app.exact(first.request.key).proof.original.state_at_record == "open"
    before = counts(app)
    service.ledger.configuration = app.configuration = None
    with app.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
    replay = StableWindowRecordingService(
        service.sessions, app, service.derivations, service.ledger
    )
    assert replay.record(POST, first.request).model_copy(update={"duplicate": False}) == first
    assert counts(app) == before
    with pytest.raises(DomainError) as error:
        replay.record(
            POST, first.request.model_copy(update={"expected_acceptance_fingerprint": "f" * 64})
        )
    assert error.value.code == "CONFLICT" and counts(app) == before


@pytest.mark.parametrize("failure", ["source", "configuration"])
def test_final_checks_roll_back_acceptance_derivation_measurement_and_all_events(
    stable_recording, monkeypatch, failure
):
    service, request, (app, _, _) = stable_recording
    before = counts(app)
    original = service.ledger.post_in_transaction

    def post(*args):
        result = original(*args)
        if failure == "configuration":
            service.ledger.configuration = None
        else:

            def unavailable(*args):
                raise DomainError("CONFLICT", "fixture original source unavailable", 409)

            monkeypatch.setattr(app, "verified_in_transaction", unavailable)
        return result

    monkeypatch.setattr(service.ledger, "post_in_transaction", post)
    with pytest.raises(DomainError):
        service.accept_and_record(POST, request)
    assert counts(app) == before


@pytest.mark.parametrize("when", ["before", "after"])
def test_commit_acknowledgement_loss_and_outer_rollback_reuse_the_first_window(
    stable_recording, monkeypatch, when
):
    import aris.modules.lyra.stable_window_recording_service as module

    service, request, (app, _, _) = stable_recording
    before = counts(app)
    with service.sessions() as db:
        provisional = service.accept_and_record_in_transaction(db, POST, request)
        assert provisional.ledger.effect == "recorded"
        db.rollback()
    assert counts(app) == before
    commit = module.commit_owned_transaction

    def interrupted(db):
        if when == "after":
            commit(db)
        raise RuntimeError("fixture lost commit acknowledgement")

    monkeypatch.setattr(module, "commit_owned_transaction", interrupted)
    with pytest.raises(RuntimeError, match="lost commit"):
        service.accept_and_record(POST, request)
    monkeypatch.setattr(module, "commit_owned_transaction", commit)
    retry = service.accept_and_record(POST, request)
    assert retry.duplicate is (when == "after")
    assert counts(app) == tuple(
        value + delta for value, delta in zip(before, (1, 3, 3, 3, 1, 1), strict=True)
    )


def test_reversal_is_not_resurrected_by_replay_or_a_different_layout(stable_recording):
    from tests.test_usage_reversals import adjustments, confirm, pending

    service, request, (app, _, fixture) = stable_recording
    first = service.accept_and_record(POST, request)
    posting = UsagePostingRequest(
        id=first.request.operation_id,
        derivation_id=first.request.derivation_id,
        session_index=first.session_index,
        expected_result_fingerprint=first.derivation_result_fingerprint,
        expected_configuration_fingerprint=request.expected_recording_configuration_fingerprint,
    )
    original_ledger = fixture[2]
    state = adjustments.__wrapped__((original_ledger[0], posting, *original_ledger[2:]))
    assert confirm(state, pending(state)).state == "succeeded"
    before = counts(app)
    assert service.record(POST, first.request).duplicate and counts(app) == before
    method = StableResidencyLayoutMethod(max_intervals=4)
    app.evidence.method = method
    different = request.model_copy(
        update={
            "window": request.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    with pytest.raises(DomainError) as error:
        service.accept_and_record(POST, different)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(app) == before


def test_postgresql_concurrent_open_and_closed_record_one_measurement(
    stable_recording, database_url
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real advisory locking")
    service, closed, (app, _, fixture) = stable_recording
    opened = open_reference(app, closed)
    before = counts(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda request: service.accept_and_record(POST, request), (opened, closed))
        )
    assert sorted(value.duplicate for value in receipts) == [False, True]
    assert receipts[0].model_copy(update={"duplicate": False}) == receipts[1].model_copy(
        update={"duplicate": False}
    )
    assert counts(app) == tuple(
        value + delta for value, delta in zip(before, (1, 3, 3, 3, 1, 1), strict=True)
    )
    # The older balanced partition also selects [10,55): reuse the exact measurement.
    reused = fixture[0].record(POST, fixture[1])
    assert reused.ledger.effect == "already_recorded"
    assert reused.ledger.entries == receipts[0].ledger.entries
    assert counts(app)[-1] == before[-1] + 1
    # A different stable layout selects [10,70): partial overlap must still fail.
    committed = counts(app)
    method = StableResidencyLayoutMethod(max_intervals=4)
    app.evidence.method = method
    overlapping = closed.model_copy(
        update={
            "window": closed.window.model_copy(
                update={"expected_method_fingerprint": method.fingerprint}
            )
        }
    )
    with pytest.raises(DomainError) as error:
        service.accept_and_record(POST, overlapping)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(app) == committed
