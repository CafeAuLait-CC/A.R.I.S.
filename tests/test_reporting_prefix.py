import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete

from aris.apps.ops import reporting_gap_history
from aris.core.access.models import RolePermission
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.reporting_gap_history_repo import ReportingGapHistoryRepository
from aris.modules.lyra.reporting_gap_history_service import (
    ReportingGapHistoryService,
    covered_reporting_position_in_transaction,
)
from aris.modules.lyra.retention_service import reporting_history_position_in_transaction
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_schemas import UsageArchiveQuery
from tests.test_activity_preview_service import SOURCE, preview_source
from tests.test_administration import admin_state
from tests.test_heartbeat import beat, heartbeat
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import ledger

__all__ = ["preview_source", "admin_state", "history_state", "derivations", "ledger", "readings"]


def initialize(state, *, early=True):
    preview, archive, actor, readings = state
    history = ReportingGapHistoryService(preview)
    assert history.scan_next(actor, SOURCE, first_seq=3).state == "caught_up"
    with archive.sessions() as db:
        live = ReportingGapHistoryRepository(db).position(SOURCE)
        sample = archive.reporting_page_in_transaction(
            db, UsageArchiveQuery(**SOURCE.model_dump(), after_seq=2, through_seq=3)
        ).reports[0]
    if early:
        for seq, seconds in ((1, 400), (2, 200)):
            observed = (sample.observed_at - timedelta(seconds=seconds)).isoformat()
            assert (
                beat(
                    readings[0][4], heartbeat(seq=seq, observed_at=observed, sent_at=observed)
                ).status_code
                == 200
            )
    return history, archive, actor, live


def test_historical_prefix_keeps_live_progress_and_resumes_without_old_payload(
    preview_source, settings, monkeypatch, tmp_path, capsys
):
    history, archive, actor, live = initialize(preview_source, early=False)
    baseline = history.latest(actor, SOURCE)
    missing = history.scan_prefix(actor, SOURCE)
    assert (missing.state, missing.through_seq, missing.missing_seq) == ("missing", 0, 1)
    with archive.sessions() as db:
        assert covered_reporting_position_in_transaction(db, SOURCE) == live
    initialize(preview_source)
    first = history.scan_prefix(actor, SOURCE, max_messages=1)
    assert (first.state, first.through_seq, first.target_through_seq) == ("more", 1, 3)
    with archive.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 1))

    def fail(*args):
        raise DomainError("RETRYABLE_DATABASE_ERROR", "Injected prefix save failure", 503)

    with monkeypatch.context() as patch:
        patch.setattr(ReportingGapHistoryRepository, "save", fail)
        with pytest.raises(DomainError, match="Injected prefix save failure"):
            history.scan_prefix(actor, SOURCE, max_messages=1)
    with archive.sessions() as db:
        assert ReportingGapHistoryRepository(db).prefix_position(SOURCE).last_report.seq == "1"
        assert ReportingGapHistoryRepository(db).latest(SOURCE, 16, None) == baseline
        assert ReportingGapHistoryRepository(db).position(SOURCE) == live
    second = history.scan_prefix(actor, SOURCE, max_messages=1)
    assert (second.state, second.through_seq, second.saved) == ("more", 2, 1)
    with archive.sessions.begin() as db:
        partial = reporting_history_position_in_transaction(db, SOURCE)
        assert partial.first_seq == 1 and partial.last_report.seq == "2"
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 2))
    final = history.scan_prefix(actor, SOURCE, max_messages=1)
    assert (final.state, final.through_seq, final.saved) == ("caught_up", 3, 1)
    assert {row.gap.next_report.seq for row in history.latest(actor, SOURCE)} == {
        "2",
        "3",
        *(row.gap.next_report.seq for row in baseline),
    }
    with archive.sessions() as db:
        combined = reporting_history_position_in_transaction(db, SOURCE)
        assert combined.first_seq == 1 and combined.last_report == live.last_report
        assert ReportingGapHistoryRepository(db).position(SOURCE) == live

    def no_original_read(*args):
        raise AssertionError("Completed historical prefix reread source data")

    with monkeypatch.context() as patch:
        patch.setattr(archive, "reporting_page_in_transaction", no_original_read)
        done = history.scan_prefix(actor, SOURCE)
    assert done.state == "caught_up" and done.saved == done.already_saved == 0
    assert history.scan_next(actor, SOURCE, first_seq=3).through_seq == int(live.last_report.seq)
    config = tmp_path / "historical-health.json"
    config.write_text(
        json.dumps(
            {
                "environment": "test",
                "database_url": settings.database_url,
                "actor_id": str(actor.actor_id),
                "source": SOURCE.model_dump(mode="json"),
            }
        )
    )
    config.chmod(0o600)
    capsys.readouterr()
    assert reporting_gap_history.main(["--config", str(config), "--scan-prefix"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "caught_up"
    with monkeypatch.context() as patch:
        patch.setenv("ARIS_DATABASE_URL", settings.database_url)
        patch.setenv("ARIS_ENVIRONMENT", "test")
        with pytest.raises(RuntimeError, match="historical reporting prefix progress"):
            command.downgrade(Config("alembic.ini"), "0051")


def test_prefix_requires_current_authority_and_rolls_back_on_revocation(
    preview_source, monkeypatch
):
    preview, archive, actor, readings = preview_source
    history = ReportingGapHistoryService(preview)
    with pytest.raises(DomainError) as absent:
        history.scan_prefix(actor, SOURCE)
    assert absent.value.code == "DEPENDENCY_UNAVAILABLE"
    history, archive, actor, live = initialize(preview_source)
    baseline = history.latest(actor, SOURCE)
    other = ActorContext(actor_id=readings[1][2], request_id="other-prefix")
    with pytest.raises(DomainError) as forbidden:
        history.scan_prefix(other, SOURCE)
    assert forbidden.value.code == "PERMISSION_DENIED"
    for maximum in (True, 0, 129):
        with pytest.raises(DomainError) as invalid:
            history.scan_prefix(actor, SOURCE, max_messages=maximum)
        assert invalid.value.code == "VALIDATION_ERROR"
    original = archive.reporting_page_in_transaction
    permissions = readings[1][3]

    def revoke(*args):
        result = original(*args)
        with archive.sessions.begin() as db:
            db.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == permissions["role"],
                    RolePermission.permission_id == permissions["all"],
                )
            )
        return result

    monkeypatch.setattr(archive, "reporting_page_in_transaction", revoke)
    with pytest.raises(DomainError) as changed:
        history.scan_prefix(actor, SOURCE)
    assert changed.value.code == "PERMISSION_DENIED"
    with archive.sessions() as db:
        repo = ReportingGapHistoryRepository(db)
        assert repo.prefix_position(SOURCE) is None and repo.latest(SOURCE, 16, None) == baseline
        assert repo.position(SOURCE) == live


def test_postgresql_prefix_compare_and_set_commits_each_range_once(preview_source, monkeypatch):
    history, archive, actor, live = initialize(preview_source)
    if archive.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires concurrent PostgreSQL historical scans")
    ready = Barrier(2, timeout=8)
    advance = ReportingGapHistoryRepository.advance_prefix

    def race(repo, *args):
        ready.wait()
        return advance(repo, *args)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
        patch.setattr(ReportingGapHistoryRepository, "advance_prefix", race)
        jobs = [workers.submit(history.scan_prefix, actor, SOURCE) for _ in range(2)]
        success = []
        for job in jobs:
            try:
                success.append(job.result(timeout=12))
            except DomainError as error:
                assert error.code == "RETRYABLE_DATABASE_ERROR"
    assert len(success) == 1 and success[0].state == "caught_up" and success[0].saved == 2
    assert history.scan_prefix(actor, SOURCE).saved == 0
    with archive.sessions() as db:
        assert ReportingGapHistoryRepository(db).position(SOURCE) == live
