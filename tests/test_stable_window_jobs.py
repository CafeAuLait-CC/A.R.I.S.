from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.modules.lyra.models import UsageRecordingJobRow
from aris.modules.lyra.recording_job_schemas import RecordingJobClaim, RecordingWorkSelection
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.stable_window_recording_schemas import StableWindowJobRequest
from aris.modules.vega.models import AgentMessage
from tests.test_recording_jobs import LIMITS
from tests.test_stable_window_acceptance import counts, open_reference
from tests.test_stable_window_recording import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    stable_recording,
)
from tests.test_usage_ledger import POST

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_recording",
]


@pytest.fixture
def stable_jobs(stable_recording):
    service, request, (app, _, fixture) = stable_recording
    accepted = app.accept(POST, open_reference(app, request))
    fixed = StableWindowJobRequest.from_acceptance(app.exact(accepted.key))
    queue = RecordingJobService(
        service.sessions,
        fixture[3],
        limits=LIMITS,
        partition_recorder=fixture[0],
        stable_recorder=service,
        clock=service.acceptances.clock,
    )
    selection = RecordingWorkSelection(
        recording_configuration_fingerprint=fixed.expected_recording_configuration_fingerprint,
        partition_method_fingerprint=fixture[0].evidence.method.fingerprint,
        stable_method_fingerprint=fixed.expected_method_fingerprint,
        include_closed_runs=False,
    )
    return queue, fixed, service, app, fixture, selection


def test_mixed_queue_skips_unconfigured_or_different_stable_work_and_reuses_exact_old_measurement(
    stable_jobs,
):
    queue, request, service, app, fixture, selection = stable_jobs
    queue.enqueue(POST, request)
    queue.enqueue(POST, fixture[1])
    before = counts(app)
    queue.stable_recorder = None
    assert queue.claim(POST, selection=selection) is None
    assert queue.exact(request.id).attempts == 0 and counts(app) == before
    queue.stable_recorder = service
    for wrong in (
        selection.model_copy(update={"stable_method_fingerprint": "f" * 64}),
        selection.model_copy(update={"recording_configuration_fingerprint": "f" * 64}),
    ):
        assert queue.claim(POST, selection=wrong) is None
    claim = queue.claim(POST, selection=selection)
    assert isinstance(claim, RecordingJobClaim) and claim.id == request.id
    result = queue.execute(POST, claim)
    assert result.state == "completed" and result.recording_id == request.id
    assert queue.exact(request.id).recording_fingerprint == result.recording_fingerprint
    old = queue.claim(
        POST,
        selection=selection.model_copy(
            update={"include_closed_runs": True, "stable_method_fingerprint": None}
        ),
    )
    assert old.id == fixture[1].id
    assert queue.execute(POST, old).state == "completed"
    assert counts(app)[-1] == before[-1] + 1
    assert queue.enqueue(POST, request).duplicate


def test_fixed_job_metadata_cannot_override_first_acceptance(stable_jobs):
    queue, request, _, app, _, _ = stable_jobs
    before = counts(app)
    for field, value in (
        ("expected_recording_configuration_fingerprint", "f" * 64),
        ("expected_method_fingerprint", "f" * 64),
        ("expected_acceptance_fingerprint", "f" * 64),
        ("key", "f" * 64),
        ("source_cutoff", request.source_cutoff + timedelta(seconds=1)),
    ):
        with pytest.raises(DomainError) as error:
            queue.enqueue(POST, request.model_copy(update={field: value}))
        assert error.value.code == "CONFLICT" and counts(app) == before
    queued = queue.enqueue(POST, request)
    assert queued.job.request == request and queued.job.state == "queued"


def test_failure_after_measurement_rolls_back_execution_and_retries_same_job(
    stable_jobs, monkeypatch
):
    queue, request, service, app, fixture, selection = stable_jobs
    queue.enqueue(POST, request)
    before = counts(app)
    original = service.record_in_transaction

    def fail(*args):
        original(*args)
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture after measurement", 503)

    with monkeypatch.context() as patch:
        patch.setattr(service, "record_in_transaction", fail)
        result = queue.execute(POST, queue.claim(POST, selection=selection))
    assert result.state == "retry" and counts(app)[-2:] == before[-2:]
    assert queue.exact(request.id).attempts == 1
    fixture[4]["now"] += timedelta(seconds=2)
    rebuilt = RecordingJobService(
        queue.sessions, queue.recorder, stable_recorder=service, limits=LIMITS, clock=queue.clock
    )
    retry = rebuilt.claim(POST, selection=selection)
    assert retry.id == request.id and retry.attempt == 2
    assert rebuilt.execute(POST, retry).state == "completed"
    assert counts(app)[-2:] == tuple(value + 1 for value in before[-2:])


def test_expired_lease_is_fenced_and_completed_history_survives_handler_pause_and_raw_retention(
    stable_jobs,
):
    queue, request, service, app, fixture, selection = stable_jobs
    queue.enqueue(POST, request)
    stale = queue.claim(POST, selection=selection)
    fixture[4]["now"] += timedelta(seconds=LIMITS.lease_seconds + 1)
    current = queue.claim(POST, selection=selection)
    assert current.id == stale.id and current.lease_token != stale.lease_token
    assert queue.execute(POST, stale).state == "lost_claim"
    result = queue.execute(POST, current)
    assert result.state == "completed"
    before = counts(app)
    queue.stable_recorder = None
    service.ledger.configuration = app.configuration = None
    with queue.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
    assert queue.exact(request.id).recording_fingerprint == result.recording_fingerprint
    assert queue.execute(POST, current) == result and counts(app) == before


def test_job_foreign_key_migration_preserves_old_rows_and_input_guards(stable_jobs, monkeypatch):
    queue, request, _, _, fixture, _ = stable_jobs
    old = queue.enqueue(POST, fixture[1])
    assert queue.execute(POST, queue.claim(POST)).state == "completed"
    previous = queue.exact(old.job.request.id)
    with monkeypatch.context() as environment:
        environment.setenv(
            "ARIS_DATABASE_URL", queue.sessions.kw["bind"].url.render_as_string(hide_password=False)
        )
        command.downgrade(Config("alembic.ini"), "0035")
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
        assert queue.exact(previous.request.id) == previous
        queue.enqueue(POST, request)
        with pytest.raises(RuntimeError, match="stable recording jobs prevent downgrade"):
            command.downgrade(Config("alembic.ini"), "0035")
    with pytest.raises(DBAPIError), queue.sessions.begin() as db:
        db.execute(update(UsageRecordingJobRow).values(canonical_request="{}"))


def test_postgresql_stable_job_execution_commits_one_result_under_competing_workers(
    stable_jobs, database_url, monkeypatch
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real row locking")
    queue, request, _, app, _, selection = stable_jobs
    queue.enqueue(POST, request)
    claim = queue.claim(POST, selection=selection)
    before = counts(app)
    entered, release = Event(), Event()
    original = queue.stable_recorder.record_in_transaction

    def held(*args):
        entered.set()
        assert release.wait(5), "competing execution did not finish its bounded lock attempt"
        return original(*args)

    monkeypatch.setattr(queue.stable_recorder, "record_in_transaction", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(queue.execute, POST, claim)
        assert entered.wait(5)
        try:
            loser = pool.submit(queue.execute, POST, claim)
            with pytest.raises(DomainError) as rejected:
                loser.result(timeout=3)
            assert rejected.value.code == "RETRYABLE_DATABASE_ERROR"
        finally:
            release.set()
        completed = winner.result(timeout=5)
    assert completed.state == "completed"
    # A bounded lock timeout is retryable; the original claim then returns the
    # committed receipt without executing or charging the measurement twice.
    assert queue.execute(POST, claim) == completed
    assert counts(app)[-2:] == tuple(value + 1 for value in before[-2:])
    assert queue.exact(request.id).attempts == 1
