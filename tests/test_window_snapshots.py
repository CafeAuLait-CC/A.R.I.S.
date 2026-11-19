from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import RoleBinding, RolePermission
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.models import UsageWindowSnapshotRow
from aris.modules.lyra.window_snapshot_schemas import CaptureUsageWindow, WindowManifestSelection
from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from tests.test_administration import admin_state
from tests.test_closed_run_recording import closed
from tests.test_heartbeat import beat, heartbeat
from tests.test_mapping_history import history_state
from tests.test_processing_coverage import capture_request, coverage, incoming
from tests.test_recorded_usage import reading_service
from tests.test_trace_consumer import CONTEXT
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, ledger
from tests.test_usage_reversals import adjustments, confirm, pending
from tests.test_usage_window import full_sources, query

__all__ = [
    "admin_state",
    "history_state",
    "derivations",
    "ledger",
    "closed",
    "coverage",
    "adjustments",
]


@pytest.fixture
def windows(coverage):
    source = coverage.service
    service = UsageWindowSnapshotService(
        source.sessions,
        source,
        source.archive,
        source.identities,
        configuration=source.configuration,
        clock=source.clock,
        timeout_ms=30000,
    )
    return SimpleNamespace(service=service, state=coverage, manifests=full_sources(coverage))


def incoming_window(state, *, manifests=None, owner=None, **changes):
    service = state.service
    with service.sessions() as session:
        source = service.archive.head_in_transaction(
            session,
            UsageArchiveHeadQuery(
                agent_id=service.configuration.trace.agent_id,
                node_id=service.configuration.trace.node_id,
            ),
        )
        head = UsageLedgerRepository(session).head(service.configuration.trace.node_id)
    value = CaptureUsageWindow(
        id=uuid4(),
        query=query(state.state, owner_id=owner),
        expected_configuration_fingerprint=service.configuration.fingerprint,
        expected_source_fingerprint=source.fingerprint,
        ledger=head,
        manifests=tuple(
            WindowManifestSelection(id=value.analysis.request.id, fingerprint=value.fingerprint)
            for value in (state.manifests if manifests is None else manifests)
        ),
    )
    return value.model_copy(update=changes)


def counts(service):
    with service.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (UsageWindowSnapshotRow, DomainEvent, OutboxEntry, AuditEntry)
        )


def postgres(service):
    if service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL consistent MVCC snapshots")


def test_captured_result_is_atomic_idempotent_and_reproducible_without_becoming_current_truth(
    windows,
):
    service = windows.service
    request = incoming_window(windows)
    before = counts(service)
    first = service.capture(
        CONTEXT.model_copy(update={"request_id": "window-" + "r" * 121}), request
    )
    assert not first.duplicate and first.snapshot.window.total_usage_microseconds == 30_000_000
    assert counts(service) == tuple(value + 1 for value in before)
    assert service.exact(request.id) == service.reverify(request.id) == first.snapshot
    assert service.capture(CONTEXT, request).duplicate
    assert counts(service) == tuple(value + 1 for value in before)
    with service.sessions() as session:
        public = session.get(DomainEvent, str(first.snapshot.event_id))
        assert "linux-user" not in str(public.payload)
    with pytest.raises(DomainError, match="different input"):
        service.capture(
            CONTEXT, request.model_copy(update={"query": query(windows.state, owner_id=uuid4())})
        )
    assert counts(service) == tuple(value + 1 for value in before)


def test_partial_coverage_is_a_saved_unknown_total_not_a_successful_zero(windows):
    request = incoming_window(windows, manifests=(windows.manifests[0],))
    saved = windows.service.capture(CONTEXT, request).snapshot
    assert not saved.window.source_prefix_complete
    assert saved.window.total_usage_microseconds is None and not saved.window.quota_usage_available
    assert saved.window.recorded_microseconds == 30_000_000
    assert windows.service.reverify(request.id) == saved


def test_snapshot_with_pending_usage_stays_reproducible_after_later_recording(coverage):
    source = coverage.service
    first = source.record(CONTEXT, incoming(coverage)).manifest
    last = source.record(
        CONTEXT,
        capture_request(
            source, coverage.last, first.analysis.request.start_at, first.analysis.request.end_at
        ),
    ).manifest
    service = UsageWindowSnapshotService(
        source.sessions,
        source,
        source.archive,
        source.identities,
        configuration=source.configuration,
        clock=source.clock,
    )
    state = SimpleNamespace(service=service, state=coverage, manifests=(first, last))
    request = incoming_window(state)
    original = service.capture(CONTEXT, request).snapshot
    assert original.window.total_usage_microseconds is None
    coverage.writer.record(POST, coverage.incoming)
    latest = source.record(CONTEXT, incoming(coverage)).manifest
    current = service.capture(CONTEXT, incoming_window(state, manifests=(latest, last))).snapshot
    assert current.window.total_usage_microseconds == 30_000_000
    assert service.reverify(request.id) == original
    assert service.capture(CONTEXT, request).snapshot == original


def test_reversal_leaves_the_old_snapshot_unchanged_and_the_new_snapshot_unknown(
    windows, adjustments
):
    service = windows.service
    request = incoming_window(windows)
    saved = service.capture(CONTEXT, request).snapshot
    assert confirm(adjustments, pending(adjustments)).state == "succeeded"
    current = service.capture(CONTEXT, incoming_window(windows)).snapshot
    assert (
        current.window.recorded_microseconds == 0
        and current.window.total_usage_microseconds is None
    )
    assert service.reverify(request.id) == saved
    assert service.reverify(current.request.id) == current


def test_read_enforces_current_owner_node_and_revoked_permissions(windows):
    service = windows.service
    _, actor, other, ids = reading_service(windows.state.ledger)
    own = service.capture(CONTEXT, incoming_window(windows, owner=actor.actor_id)).snapshot
    node = service.capture(CONTEXT, incoming_window(windows)).snapshot
    assert service.read(actor, own.request.id).snapshot_fingerprint == own.fingerprint
    assert service.read(actor, node.request.id).snapshot_fingerprint == node.fingerprint
    with pytest.raises(DomainError) as error:
        service.read(ActorContext(actor_id=other, request_id="fixture-other"), own.request.id)
    assert error.value.code == "PERMISSION_DENIED"
    with service.sessions.begin() as session:
        session.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
    assert service.read(actor, own.request.id).snapshot_fingerprint == own.fingerprint
    with pytest.raises(DomainError) as error:
        service.read(actor, node.request.id)
    assert error.value.code == "PERMISSION_DENIED"
    with service.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))
    with pytest.raises(DomainError) as error:
        service.read(actor, own.request.id)
    assert error.value.code == "PERMISSION_DENIED"


def test_saved_read_does_not_rescan_originals_but_explicit_reverification_detects_their_loss(
    windows,
):
    service = windows.service
    _, actor, _, _ = reading_service(windows.state.ledger)
    result = service.capture(CONTEXT, incoming_window(windows, owner=actor.actor_id)).snapshot
    with service.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 5))
    assert service.read(actor, result.request.id).snapshot_fingerprint == result.fingerprint
    with pytest.raises(DomainError):
        service.reverify(result.request.id)


def test_disabled_capture_does_not_query_or_mutate_the_database(windows):
    service, request = windows.service, incoming_window(windows)
    service.configuration = None

    def reject(*args, **kwargs):
        raise AssertionError("disabled snapshot touched database")

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(DomainError, match="not enabled"):
            service.capture(CONTEXT, request)
    finally:
        event.remove(engine, "before_cursor_execute", reject)


def test_stale_boundaries_and_wrong_manifest_fingerprint_do_not_create_history(windows):
    service = windows.service
    original, before = incoming_window(windows), counts(service)
    bad = [
        original.model_copy(update={"expected_source_fingerprint": "f" * 64}),
        original.model_copy(update={"expected_configuration_fingerprint": "f" * 64}),
        original.model_copy(
            update={
                "manifests": (original.manifests[0].model_copy(update={"fingerprint": "f" * 64}),)
            }
        ),
    ]
    for request in bad:
        with pytest.raises(DomainError):
            service.capture(CONTEXT, request)
        assert counts(service) == before


def test_final_insert_failure_keeps_snapshot_event_outbox_and_audit_atomic(windows):
    service = windows.service
    request, before = incoming_window(windows), counts(service)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO usage_window_snapshots"):
            raise RuntimeError("fixture snapshot insertion failure")

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="fixture snapshot"):
            service.capture(CONTEXT, request)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(service) == before


def test_snapshot_immutability_and_downgrade_protection(windows):
    service = windows.service
    saved = service.capture(CONTEXT, incoming_window(windows)).snapshot
    for statement in (
        "UPDATE usage_window_snapshots SET fingerprint='x'",
        "DELETE FROM usage_window_snapshots",
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as session:
            session.execute(text(statement))
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0025")
    assert service.exact(saved.request.id) == saved


def test_empty_snapshot_migration_roundtrip_matches_metadata(windows):
    command.check(Config("alembic.ini"))
    command.downgrade(Config("alembic.ini"), "0025")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    assert (
        windows.service.capture(CONTEXT, incoming_window(windows)).snapshot.window.coverage
        == "complete"
    )


def test_postgresql_capture_does_not_block_new_source_arrivals_or_mix_them_into_its_fixed_view(
    windows, monkeypatch
):
    service = windows.service
    postgres(service)
    request = incoming_window(windows)
    original = service.archive.head_in_transaction
    calls = []

    def advancing(session, query):
        result = original(session, query)
        if not calls:
            calls.append(True)
            assert beat(windows.state.ledger[4], heartbeat(seq=10)).status_code == 200
        return result

    monkeypatch.setattr(service.archive, "head_in_transaction", advancing)
    saved = service.capture(CONTEXT, request).snapshot
    assert saved.window.source.highest_committed_seq == 9
    assert saved.window.total_usage_microseconds == 30_000_000
    with service.sessions() as session:
        assert original(session, saved.window.source.query).highest_committed_seq == 10
    assert service.reverify(request.id) == saved


def test_postgresql_same_key_can_retry_serialization_and_commits_exactly_once(windows):
    service = windows.service
    postgres(service)
    request, before, barrier = incoming_window(windows), counts(service), Barrier(2)

    def capture():
        barrier.wait()
        try:
            return service.capture(CONTEXT, request)
        except DomainError as error:
            assert error.code == "RETRYABLE_DATABASE_ERROR"
            return service.capture(CONTEXT, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: capture(), range(2)))
    assert sorted(value.duplicate for value in results) == [False, True]
    assert results[0].snapshot == results[1].snapshot
    assert counts(service) == tuple(value + 1 for value in before)
