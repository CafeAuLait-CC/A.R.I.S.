from datetime import timedelta
from time import monotonic
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsagePostingRequest
from aris.modules.lyra.models import StableRecordingLinkRow, UsageTraceAttestationRow
from aris.modules.lyra.stable_window_recording_repo import StableWindowRecordingRepository
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.lyra.window_snapshot_inputs import window_identities
from aris.modules.lyra.window_snapshot_schemas import CaptureUsageWindow, WindowManifestSelection
from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from tests.test_administration import NOW
from tests.test_mapping_history import assign
from tests.test_processing_coverage import capture_request
from tests.test_stable_window_recording import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    open_reference,
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


def service_for(writer):
    source = writer.acceptances.evidence.originals
    config = ProcessingCoverageConfiguration(
        trace=source.records.configuration,
        recording=writer.ledger.configuration,
        gpu_uuids=("GPU-a",),
    )
    return ProcessingCoverageService(
        writer.sessions,
        source.attestations,
        source.archive,
        writer.derivations.observations,
        writer.acceptances.identities,
        configuration=config,
        clock=source.clock,
    )


def manifests_for(service):
    with service.sessions() as db:
        ids = tuple(
            db.scalars(
                select(UsageTraceAttestationRow.id).order_by(UsageTraceAttestationRow.revision)
            )
        )
    return tuple(
        service.record(
            POST,
            capture_request(
                service,
                service.attestations.exact(id),
                NOW + timedelta(seconds=10),
                NOW + timedelta(seconds=145),
            ),
        ).manifest
        for id in ids
    )


def window_for(service, manifests):
    window_service = UsageWindowSnapshotService(
        service.sessions,
        service,
        service.archive,
        service.identities,
        configuration=service.configuration,
        clock=service.clock,
        timeout_ms=30000,
    )
    with service.sessions() as db:
        head = UsageLedgerRepository(db).head("node-a")
        source = service.archive.head_in_transaction(
            db, UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")
        )
    request = CaptureUsageWindow(
        id=uuid4(),
        query=UsageWindowQuery(
            node_id="node-a",
            start_at=NOW + timedelta(seconds=10),
            end_at=NOW + timedelta(seconds=145),
        ),
        expected_configuration_fingerprint=service.configuration.fingerprint,
        expected_source_fingerprint=source.fingerprint,
        ledger=head,
        manifests=tuple(
            WindowManifestSelection(id=value.analysis.request.id, fingerprint=value.fingerprint)
            for value in manifests
        ),
    )
    result = window_service.capture(POST, request).snapshot
    assert window_service.reverify(request.id) == result
    return result


def post_remaining(writer, closed):
    return tuple(
        writer.accept_and_record(
            POST,
            closed.model_copy(
                update={"window": closed.window.model_copy(update={"window_index": index})}
            ),
        )
        for index in (1, 2)
    )


def test_live_prefix_and_closed_tails_form_reproducible_complete_sampled_window(stable_recording):
    writer, closed, (app, _, fixture) = stable_recording
    first = writer.accept_and_record(POST, open_reference(app, closed))
    service = service_for(writer)
    pending = manifests_for(service)
    partial = window_for(service, pending)
    assert partial.window.recorded_microseconds == 45_000_000
    assert partial.window.verified_usage_microseconds == 45_000_000
    assert partial.window.total_usage_microseconds is None
    assert partial.window.unknown_gpu_microseconds == 90_000_000
    assert {
        cell.reason
        for value in pending
        for cell in value.analysis.cells
        if cell.state == "recorded"
    } == {"COMMITTED_STABLE_RECORDING"}
    post_remaining(writer, closed)
    complete = window_for(service, manifests_for(service))
    assert complete.window.coverage == "complete"
    assert complete.window.total_usage_microseconds == 135_000_000
    for value in pending:
        assert service.reverify(value.analysis.request.id) == value
    with writer.sessions() as db:
        evidence = StableWindowRecordingRepository().evidence_for_operation(db, first.id)
        assert evidence.recording == first
        assert db.scalar(select(func.count()).select_from(StableRecordingLinkRow)) == 3
    from tests.test_usage_reversals import adjustments, confirm
    from tests.test_usage_reversals import pending as reversal_request

    posting = UsagePostingRequest(
        id=first.id,
        derivation_id=first.request.derivation_id,
        session_index=first.session_index,
        expected_result_fingerprint=first.derivation_result_fingerprint,
        expected_configuration_fingerprint=closed.expected_recording_configuration_fingerprint,
    )
    original_ledger = fixture[2]
    review = adjustments.__wrapped__((original_ledger[0], posting, *original_ledger[2:]))
    assert confirm(review, reversal_request(review)).state == "succeeded"
    current = window_for(service, manifests_for(service)).window
    assert current.verified_usage_microseconds == 90_000_000
    assert current.unknown_gpu_microseconds == 45_000_000
    historical = UsageWindowSnapshotService(
        service.sessions,
        service,
        service.archive,
        service.identities,
        clock=service.clock,
        timeout_ms=30000,
    )
    assert historical.reverify(complete.request.id) == complete


@pytest.mark.parametrize("changed_at,known", [(130, 45_000_000), (80, 0)])
def test_new_mapping_knowledge_assesses_each_first_parent_without_rewriting_history(
    stable_recording,
    changed_at,
    known,
):
    writer, closed, (app, _, fixture) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    post_remaining(writer, closed)
    service = service_for(writer)
    original = manifests_for(service)
    assert window_for(service, original).window.total_usage_microseconds == 135_000_000
    history, clock = fixture[2][2][2], fixture[4]
    present = clock["now"]
    assign(history, changed_at, 1, expected=history[4][0])
    clock["now"] = present
    current = manifests_for(service)
    result = window_for(service, current)
    assert result.window.recorded_microseconds == 135_000_000
    assert result.window.verified_usage_microseconds == known
    assert result.window.unknown_gpu_microseconds == 135_000_000 - known
    assert result.window.total_usage_microseconds is None
    for value in original:
        assert service.reverify(value.analysis.request.id) == value
    # Combining old manifests with today's directory still assesses each accepted parent.
    assert window_for(service, original).window.verified_usage_microseconds == known


def test_missing_link_does_not_create_coverage_and_link_failure_is_atomic(
    stable_recording,
    monkeypatch,
):
    writer, closed, (app, _, _) = stable_recording
    original = StableWindowRecordingRepository.link

    def fail(db, receipt):
        original(db, receipt)
        raise RuntimeError("fixture link failure after insert")

    with monkeypatch.context() as patch:
        patch.setattr(StableWindowRecordingRepository, "link", staticmethod(fail))
        with pytest.raises(RuntimeError, match="link failure"):
            writer.accept_and_record(POST, open_reference(app, closed))
    with writer.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StableRecordingLinkRow)) == 0
        assert UsageLedgerRepository(db).head("node-a").revision == 0
    first = writer.accept_and_record(POST, open_reference(app, closed))
    service = service_for(writer)
    with monkeypatch.context() as patch:
        patch.setattr(StableWindowRecordingRepository, "for_entry", lambda *a, **kw: None)
        assert window_for(service, manifests_for(service)).window.verified_usage_microseconds == 0
    with writer.sessions() as db:
        assert StableWindowRecordingRepository().evidence_for_operation(db, first.id) is not None
    for operation in ("UPDATE", "DELETE"):
        statement = (
            "UPDATE usage_stable_recording_links SET entry_id=entry_id"
            if operation == "UPDATE"
            else "DELETE FROM usage_stable_recording_links"
        )
        with pytest.raises(DBAPIError), writer.sessions.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0038")


def test_postgresql_stable_reverification_and_identity_inputs_are_read_only(
    stable_recording,
    database_url,
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real read-only snapshots")
    writer, closed, (app, _, _) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    post_remaining(writer, closed)
    service = service_for(writer)
    manifests = manifests_for(service)
    with writer.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        for value in manifests:
            assert service.reverify_in_transaction(db, value.analysis.request.id) == value
        revision = manifests[0].analysis.identity_revision
        answers = window_identities(
            db, service.identities, manifests, revision, service._now(), monotonic() + 20
        )
        assert len(answers) == 2 and len({value.run.id for value in answers}) == 1


@pytest.mark.parametrize("legacy_first", [False, True])
def test_nonempty_schema38_receipts_backfill_without_changing_original_results(
    stable_recording,
    monkeypatch,
    legacy_first,
):
    writer, closed, (app, _, fixture) = stable_recording
    command.downgrade(Config("alembic.ini"), "0038")
    if legacy_first:
        fixture[0].record(POST, fixture[1])
    # Model only the old schema38 writer: same receipt, no new derived index call.
    with monkeypatch.context() as patch:
        patch.setattr(StableWindowRecordingRepository, "link", lambda *args: None)
        receipts = (
            writer.accept_and_record(POST, open_reference(app, closed)),
            *post_remaining(writer, closed),
        )
    command.upgrade(Config("alembic.ini"), "0039")
    command.check(Config("alembic.ini"))
    with writer.sessions() as db:
        assert db.scalar(select(func.count()).select_from(StableRecordingLinkRow)) == 3
        for receipt in receipts:
            assert (
                StableWindowRecordingRepository().evidence_for_operation(db, receipt.id).recording
                == receipt
            )
    assert (
        window_for(
            service_for(writer), manifests_for(service_for(writer))
        ).window.total_usage_microseconds
        == 135_000_000
    )
