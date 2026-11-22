import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError

from aris.apps.ops import reporting_gap_history
from aris.core.access.models import RolePermission
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.reporting_gap_history_models import (
    ReportingGapHistoryRow,
    ReportingGapScanRow,
)
from aris.modules.lyra.reporting_gap_history_repo import ReportingGapHistoryRepository
from aris.modules.lyra.reporting_gap_history_service import ReportingGapHistoryService
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from tests.test_activity_preview import RULE
from tests.test_administration import admin_state
from tests.test_heartbeat import beat, heartbeat
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import counts, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings"]
SOURCE = UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")


def test_reporting_worker_paused_config_does_not_open_a_database(tmp_path, monkeypatch, capsys):
    config = tmp_path / "health-worker.json"
    config.write_text(
        json.dumps(
            {
                "environment": "test",
                "database_url": "sqlite+pysqlite:///unused-health-worker.sqlite",
                "actor_id": "6a57b186-efbf-4a5e-940a-737fea84ac34",
                "sources": [{"source": SOURCE.model_dump(mode="json"), "first_seq": 1}],
            }
        )
    )
    config.chmod(0o600)

    def unexpected_connection(*args):
        raise AssertionError("Paused worker connected to a database")

    monkeypatch.setattr(reporting_gap_history, "build_engine", unexpected_connection)
    assert reporting_gap_history.main(["--config", str(config), "--run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "paused", "database_accessed": False}


@pytest.fixture
def preview_source(readings):
    _, (original, actor, _, _), _ = readings
    source = VegaUsageArchive(
        original.sessions, clock=lambda: datetime.now(UTC) + timedelta(seconds=1)
    )
    return ActivityPreviewService(original.sessions, source), source, actor, readings


def read(state, **changes):
    service, _, actor, _ = state
    return service.recent(actor, SOURCE, RULE, max_messages=changes.get("max_messages", 5))


def test_activity_preview_reads_original_archive_without_ledger_or_mapping_queries(preview_source):
    service, source, _, readings = preview_source
    baseline = counts(readings[0])
    statements = []

    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement.lower())

    engine = source.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        value = read(preview_source)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert (
        value.source_packets == 5 and value.telemetry_packets == 3 and value.heartbeat_packets == 2
    )
    assert len(value.groups) == 1 and value.groups[0].state == "reported_busy"
    assert value.requested_after_seq == 2
    assert (
        value.range_complete
        and not value.retention_enabled
        and value.prior_source_context == "not_loaded"
    )
    assert not any(
        "usage_ledger" in statement or "node_user_mapping" in statement for statement in statements
    )
    assert counts(readings[0]) == baseline


def test_activity_preview_requires_current_all_permission_and_rechecks_after_snapshot(
    preview_source, monkeypatch
):
    service, source, actor, readings = preview_source
    _, (_, _, other, ids), _ = readings
    with pytest.raises(DomainError) as error:
        service.recent(
            ActorContext(actor_id=other, request_id="other-fixture"), SOURCE, RULE, max_messages=128
        )
    assert error.value.code == "PERMISSION_DENIED"
    original = source.page_in_transaction

    def revoke(session, query):
        value = original(session, query)
        with source.sessions.begin() as other_session:
            other_session.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == ids["role"],
                    RolePermission.permission_id == ids["all"],
                )
            )
        return value

    monkeypatch.setattr(source, "page_in_transaction", revoke)
    with pytest.raises(DomainError) as error:
        read(preview_source)
    assert error.value.code == "PERMISSION_DENIED"

    def forbidden(*args, **kwargs):
        raise AssertionError("own-only permission read original activity bodies")

    monkeypatch.setattr(source, "head_in_transaction", forbidden)
    with pytest.raises(DomainError) as error:
        read(preview_source)
    assert error.value.code == "PERMISSION_DENIED"


def test_activity_archive_gap_and_corruption_do_not_become_complete_or_idle(preview_source):
    _, source, _, _ = preview_source
    broad = read(preview_source, max_messages=128)
    assert broad.missing_seq == 1 and broad.source_packets == 0
    with source.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 4))
    gap = read(preview_source)
    assert gap.missing_seq == 4 and not gap.range_complete and gap.source_packets == 1
    assert gap.groups[0].state == "reported_busy"
    with source.sessions.begin() as session:
        session.execute(
            update(AgentMessage).where(AgentMessage.seq == 3).values(fingerprint="f" * 64)
        )
    with pytest.raises(DomainError) as error:
        read(preview_source)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_reporting_gap_history_only_keeps_proved_abnormal_pairs(preview_source, monkeypatch):
    preview, source, actor, readings = preview_source
    history = ReportingGapHistoryService(preview)
    _, (_, _, other, _), _ = readings
    caller = readings[0][4]
    clock = readings[0][2][2][5]
    start = clock["now"] - timedelta(minutes=20)
    for seq, elapsed in ((8, 0), (9, 15), (10, 198)):
        observed = start + timedelta(seconds=elapsed)
        assert (
            beat(
                caller,
                heartbeat(seq=seq, observed_at=observed.isoformat(), sent_at=observed.isoformat()),
            ).status_code
            == 200
        )

    def capture(through, messages):
        return history.capture(actor, SOURCE, through_seq=through, max_messages=messages)

    normal = capture(9, 2)
    assert normal.source_range_complete and normal.saved == normal.already_saved == 0
    assert not history.latest(actor, SOURCE)
    if source.sessions.kw["bind"].dialect.name == "postgresql":
        ready = Barrier(2, timeout=10)
        insert = ReportingGapHistoryRepository.save

        def simultaneous(repo, *args):
            ready.wait()
            return insert(repo, *args)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
            patch.setattr(ReportingGapHistoryRepository, "save", simultaneous)
            pending = [workers.submit(capture, 10, 3) for _ in range(2)]
            saved = []
            for task in pending:
                try:
                    saved.append(task.result(timeout=15))
                except DomainError as error:
                    assert error.code == "RETRYABLE_DATABASE_ERROR"
        assert sum(value.saved for value in saved) == 1
        assert all(value.source_range_complete for value in saved)
    else:
        first = capture(10, 3)
        assert first.source_range_complete and first.saved == 1 and first.already_saved == 0
    (record,) = history.latest(actor, SOURCE)
    assert record.gap.previous_report.seq == "9"
    assert record.gap.next_report.seq == "10"
    assert record.gap.large_observed_interval and not record.gap.large_received_interval
    assert record.gap.cause == "unknown"
    retry = capture(10, 2)
    assert retry.saved == 0 and retry.already_saved == 1
    assert history.latest(actor, SOURCE) == (record,)
    assert not history.latest(actor, SOURCE, before_seq=10)
    with pytest.raises(IntegrityError), source.sessions.begin() as session:
        session.execute(update(ReportingGapHistoryRow).values(canonical_record="changed"))
    with source.sessions.begin() as session:
        session.execute(delete(AgentMessage).where(AgentMessage.seq.in_((9, 10))))
    assert history.latest(actor, SOURCE) == (record,)
    with pytest.raises(DomainError) as refused:
        history.latest(ActorContext(actor_id=other, request_id="history-check"), SOURCE)
    assert refused.value.code == "PERMISSION_DENIED"


def test_reporting_gap_scan_resumes_across_batches_and_rolls_back_position(
    preview_source, monkeypatch
):
    preview, source, actor, readings = preview_source
    start = readings[0][2][2][5]["now"] - timedelta(minutes=20)
    for seq, elapsed in ((8, 0), (9, 15), (10, 198), (12, 228)):
        observed = start + timedelta(seconds=elapsed)
        assert (
            beat(
                readings[0][4],
                heartbeat(seq=seq, observed_at=observed.isoformat(), sent_at=observed.isoformat()),
            ).status_code
            == 200
        )

    def scan(first_seq=8, max_messages=2):
        history = ReportingGapHistoryService(preview)
        return history.scan_next(actor, SOURCE, first_seq=first_seq, max_messages=max_messages)

    initial = scan()
    assert (initial.state, initial.through_seq, initial.saved) == ("more", 9, 0)
    with source.sessions.begin() as session:
        anchor_time = session.scalar(select(AgentMessage.received_at).where(AgentMessage.seq == 9))
        session.execute(
            update(AgentMessage)
            .where(AgentMessage.seq == 10)
            .values(received_at=anchor_time - timedelta(seconds=1))
        )
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 9))

    def fail(*args):
        raise DomainError("RETRYABLE_DATABASE_ERROR", "Injected reporting history failure", 503)

    with monkeypatch.context() as patch:
        patch.setattr(ReportingGapHistoryRepository, "save", fail)
        with pytest.raises(DomainError, match="Injected reporting history failure"):
            scan()
    with source.sessions() as session:
        assert session.get(ReportingGapScanRow, ("node-a", "agent-a")).through_seq == 9

    resumed = scan()
    assert (resumed.state, resumed.through_seq, resumed.saved, resumed.missing_seq) == (
        "missing",
        10,
        1,
        11,
    )
    assert scan().through_seq == 10
    (record,) = ReportingGapHistoryService(preview).latest(actor, SOURCE)
    assert (record.gap.previous_report.seq, record.gap.next_report.seq) == ("9", "10")
    assert record.gap.large_observed_interval and not record.gap.large_received_interval
    assert resumed.uncertain_clock_pairs == 1
    with pytest.raises(DomainError) as wrong_start:
        scan(first_seq=10)
    assert wrong_start.value.code == "CONFLICT"

    observed = start + timedelta(seconds=213)
    assert (
        beat(
            readings[0][4],
            heartbeat(seq=11, observed_at=observed.isoformat(), sent_at=observed.isoformat()),
        ).status_code
        == 200
    )
    continued = scan()
    assert (continued.state, continued.through_seq, continued.saved) == ("caught_up", 12, 0)
    assert scan() == continued
    with source.sessions() as session:
        assert len(tuple(session.scalars(select(ReportingGapScanRow)))) == 1


def test_postgresql_reporting_gap_scan_compare_and_set_does_not_double_advance(
    preview_source, monkeypatch
):
    preview, source, actor, _ = preview_source
    if source.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires concurrent PostgreSQL reports and cursor comparators")
    ready = Barrier(2, timeout=8)
    advance = ReportingGapHistoryRepository.advance

    def race(repo, *args):
        ready.wait()
        return advance(repo, *args)

    history = ReportingGapHistoryService(preview)
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
        patch.setattr(ReportingGapHistoryRepository, "advance", race)
        pending = [workers.submit(history.scan_next, actor, SOURCE, first_seq=3) for _ in range(2)]
        successful = []
        for task in pending:
            try:
                successful.append(task.result(timeout=12))
            except DomainError as error:
                assert error.code == "RETRYABLE_DATABASE_ERROR"
    assert len(successful) == 1 and successful[0].state == "caught_up"
    retry = history.scan_next(actor, SOURCE, first_seq=3)
    assert retry.through_seq == successful[0].through_seq
    assert retry.uncertain_clock_pairs == successful[0].uncertain_clock_pairs
    assert retry.state == "caught_up" and retry.saved == retry.already_saved == 0
    assert len(history.latest(actor, SOURCE)) == successful[0].saved


def test_postgresql_activity_preview_fixed_snapshot_ignores_later_gap_fill_and_recovers_after_lock(
    preview_source, monkeypatch
):
    _, source, _, readings = preview_source
    if source.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL repeatable source snapshot and real lock")
    with source.sessions.begin() as session:
        original_payload = session.scalar(select(AgentMessage.payload).where(AgentMessage.seq == 4))
        session.execute(delete(AgentMessage).where(AgentMessage.seq == 4))
    original_head = source.head_in_transaction
    first = True

    def restored(session, query):
        nonlocal first
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        value = original_head(session, query)
        if first:
            first = False
            assert beat(readings[0][4], original_payload).status_code == 200
        return value

    monkeypatch.setattr(source, "head_in_transaction", restored)
    gap = read(preview_source)
    assert gap.missing_seq == 4 and not gap.range_complete
    assert read(preview_source).range_complete
    before = counts(readings[0])
    with source.sessions() as blocker:
        blocker.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            read(preview_source)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert read(preview_source).range_complete and counts(readings[0]) == before


def test_selected_older_window_is_stable_when_new_packets_arrive_and_gaps_are_not_skipped(
    preview_source, client
):
    from aris.modules.vega.gpu_hardware_service import VegaGpuHardwareObservations
    from tests.test_resource_telemetry import put, resource_batch

    reader, source, actor, _ = preview_source
    old = reader.recent(actor, SOURCE, RULE, max_messages=3, through_seq=5)
    assert old.highest_committed_seq == 7 and old.selected_through_seq == 5
    assert old.requested_after_seq == 2 and old.last_read_seq == 5 and old.range_complete
    assert old.telemetry_packets == 2 and old.heartbeat_packets == 1
    assert put(client, resource_batch(seq=8)).status_code == 200
    fixed = reader.recent(actor, SOURCE, RULE, max_messages=3, through_seq=5)
    assert fixed.highest_committed_seq == 8 and fixed.selected_through_seq == 5
    assert fixed.groups == old.groups and fixed.source_packets == 3
    latest = reader.recent(actor, SOURCE, RULE, max_messages=3)
    assert latest.selected_through_seq == 8 and latest.requested_after_seq == 5
    reader.hardware_source = VegaGpuHardwareObservations(source.sessions)
    combined = reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=3, through_seq=5)
    assert combined.preview.groups == old.groups and combined.hardware.telemetry_frames == 2
    assert combined.sampling_incidents is None
    assert combined.reporting is None
    diagnosed = reader.recent_with_hardware(
        actor, SOURCE, RULE, max_messages=3, through_seq=5, include_incidents=True
    )
    assert diagnosed.sampling_incidents == ()
    assert diagnosed.reporting is not None and diagnosed.reporting.source_range_complete
    assert diagnosed.reporting.source_preview_fingerprint == diagnosed.preview.fingerprint
    gap = reader.recent(actor, SOURCE, RULE, max_messages=3, through_seq=2)
    assert gap.missing_seq == 1 and gap.source_packets == 0 and not gap.range_complete
    for end in (0, True, 9, 9223372036854775808):
        with pytest.raises(DomainError) as error:
            reader.recent(actor, SOURCE, RULE, max_messages=3, through_seq=end)
        assert error.value.code == "VALIDATION_ERROR"
