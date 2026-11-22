import csv
import io
import json
import os
import select as io_ready
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.apps.api.main import create_app
from aris.apps.composition.daily_coverage import prepare_day_revision, verify_daily_coverage
from aris.apps.composition.lyra import build_lyra_components
from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.apps.composition.weekly_retention import preview_week_retention
from aris.apps.ops.weekly_coverage import WeeklyCoverageConfig, main, run_tick
from aris.core.access.models import ExternalIdentity, Permission, Role, RoleBinding, RolePermission
from aris.core.access.schemas import ExternalSubject
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.scheduler.schemas import SchedulerContext
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.combined_week_export import combined_week_csv, combined_week_html
from aris.interfaces.usage.service import UsageHistorySurface
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.combined_coverage_week import combine_coverage_week
from aris.modules.nova.coverage_schedule_models import (
    CoveragePeriodResultRow,
    CoverageScheduleRow,
    DailyCoveragePeriodResultRow,
)
from aris.modules.nova.coverage_schedule_reader import CoverageScheduleReader
from aris.modules.nova.coverage_schedule_repo import CoverageScheduleRepository
from aris.modules.nova.coverage_schedule_schemas import (
    CoverageScheduleDefinition,
    DailyCoverageEvidence,
    DailyCoveragePeriodResult,
)
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.daily_coverage_calendar import daily_coverage_targets
from aris.modules.nova.report_calendar import WeeklyReportPlan, weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target
from aris.modules.vega.models import AgentMessage
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership
from tests.test_recorded_usage import reading_service
from tests.test_resource_telemetry import put, resource_batch
from tests.test_stable_recording_worker import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    stable_config,
)

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]
CONTEXT = SchedulerContext(request_id="fixture-weekly-coverage")


@pytest.mark.skipif(os.name != "posix", reason="native Unix signal contract")
def test_native_idle_worker_stops_with_single_and_repeated_signals(
    partition_recording, settings, tmp_path
):
    today = datetime.now(UTC).date()
    future = today + timedelta(days=7 - today.weekday())
    schedules, _, definition, _, _ = setup(partition_recording, settings, first_week=future)
    personal = definition.model_copy(
        update={"calendar": definition.calendar.model_copy(update={"report_type": "usage.mine"})}
    )
    config = WeeklyCoverageConfig(
        enabled=True,
        environment="test",
        database_url=settings.database_url,
        plans=(definition, personal),
        poll_seconds=60,
    )
    path = tmp_path / "weekly-native.json"
    path.write_text(
        json.dumps(
            {
                **config.model_dump(mode="json"),
                "database_url": config.database_url.get_secret_value(),
            }
        )
    )
    path.chmod(0o600)
    with schedules.sessions() as db:
        before = UsageLedgerRepository(db).head("node-a")
    root = Path(__file__).resolve().parents[1]
    for number, (received, count) in enumerate(
        ((signal.SIGTERM, 1), (signal.SIGINT, 1), (signal.SIGTERM, 256), (signal.SIGINT, 256))
    ):
        stack = tmp_path / f"signal-{number}.log"
        code = (
            "import faulthandler,signal,runpy,sys; f=open(sys.argv[2],'ab'); "
            "faulthandler.register(signal.SIGUSR1,file=f,all_threads=True); "
            "sys.argv=['weekly','--config',sys.argv[1],'--run']; "
            "runpy.run_module('aris.apps.ops.weekly_coverage',run_name='__main__')"
        )
        process = subprocess.Popen(
            [
                str(root / "scripts/uv"),
                "run",
                "--no-sync",
                "python",
                "-c",
                code,
                str(path),
                str(stack),
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        target = None
        try:
            assert io_ready.select([process.stdout], [], [], 15)[0], (
                "native worker did not report ready"
            )
            first = json.loads(process.stdout.readline())
            assert all(item["state"] == "caught_up" for item in first["plans"])
            candidates = []
            for row in subprocess.check_output(
                ["ps", "-axo", "pid=,pgid=,uid=,command="], text=True
            ).splitlines():
                parts = row.split(maxsplit=3)
                if (
                    len(parts) == 4
                    and int(parts[1]) == process.pid
                    and int(parts[2]) == os.getuid()
                    and str(path) in parts[3]
                    and "faulthandler" in parts[3]
                    and "python" in parts[3].split()[0].lower()
                ):
                    candidates.append(int(parts[0]))
            assert len(candidates) == 1
            target = candidates[0]
            began = monotonic()
            for _ in range(count):
                try:
                    if os.getpgid(target) != process.pid:
                        break
                    os.kill(target, received)
                except ProcessLookupError:
                    break
            try:
                _, error = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                if target is not None and os.getpgid(target) == process.pid:
                    os.kill(target, signal.SIGUSR1)
                raise
            assert process.returncode == 0, error
            assert monotonic() - began < 3, "shutdown must interrupt a 60-second idle wait"
        finally:
            if process.poll() is None:
                try:
                    group = os.getpgid(process.pid)
                except ProcessLookupError:
                    pass
                else:
                    assert group == process.pid
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
    with schedules.sessions() as db:
        assert UsageLedgerRepository(db).head("node-a") == before
        assert db.scalar(select(func.count()).select_from(CoverageScheduleRow)) == 2
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 0


def setup(state, settings, *, first_week=date(2026, 11, 16)):
    _, actor, _, _ = reading_service(state[2])
    clock = state[4]
    clock["now"] = max(clock["now"], datetime(2026, 11, 23, 1, tzinfo=UTC))
    runtime = stable_config(state)
    graph = build_lyra_components(state[0].sessions, runtime, clock=lambda: clock["now"])
    for _ in range(8):
        assert not graph.run_once()["failures"]
    producer = graph.pump.pipelines[0]
    cfg = ProcessingCoverageConfiguration(
        method="indexed_sampled_residency_edges",
        trace=producer.configuration.trace,
        recording=runtime.recording,
        gpu_uuids=("GPU-a",),
    )
    calendar = WeeklyReportPlan(
        namespace="fixture-weekly-coverage",
        report_type="usage.node",
        owner_id=actor.actor_id,
        scope={"type": "node", "id": "node-a"},
        timezone="UTC",
        first_week=first_week,
    )
    definition = CoverageScheduleDefinition(calendar=calendar, configuration=cfg)
    config = WeeklyCoverageConfig(
        enabled=True,
        environment="test",
        database_url=settings.database_url,
        plans=(definition,),
        transaction_timeout_ms=30000,
    )
    schedules = CoverageScheduleService(state[0].sessions, enabled=True, clock=lambda: clock["now"])
    return schedules, config, definition, clock, graph


def finite(schedules, definition, week):
    period = weekly_report_period(definition.calendar, week)
    target = weekly_coverage_target(
        definition.calendar, period, definition.configuration, now=schedules._now()
    )
    return build_lyra_coverage_components(
        schedules.sessions,
        LyraCoverageRuntimeConfig(
            enabled=True,
            configuration=definition.configuration,
            target=CoverageBuildTarget(id=target.id, query=target.query),
            prepare_prefix=True,
        ),
        clock=schedules.clock,
    )


def drain(schedules, config):
    for _ in range(16):
        value = run_tick(config, schedules, clock=schedules.clock)
        assert not value["needs_attention"], value
        item = value["plans"][0]
        if item["state"] == "snapshot_saved":
            return item["result"]
    pytest.fail("finite weekly steps did not finish")


def test_disabled_validate_and_status_do_not_create_plans_or_databases(
    tmp_path, monkeypatch, capsys
):
    import aris.apps.ops.weekly_coverage as operator

    path = tmp_path / "weekly-private.json"
    database = tmp_path / "missing.db"
    cfg = WeeklyCoverageConfig(database_url="sqlite+pysqlite:///" + str(database))
    path.write_text(
        json.dumps(
            {**cfg.model_dump(mode="json"), "database_url": cfg.database_url.get_secret_value()}
        )
    )
    path.chmod(0o600)
    monkeypatch.setattr(
        operator, "build_engine", lambda *_: pytest.fail("disabled/validation connected")
    )
    assert main(["--config", str(path), "--once"]) == 0
    assert not json.loads(capsys.readouterr().out)["database_accessed"]
    assert main(["--config", str(path), "--validate"]) == 0
    assert not json.loads(capsys.readouterr().out)["database_accessed"]
    assert main(["--config", str(path), "--status"]) == 2
    assert "DEPENDENCY_UNAVAILABLE" in capsys.readouterr().err
    assert not database.exists()
    path.chmod(0o644)
    assert main(["--config", str(path), "--validate"]) == 2
    assert "CONFIGURATION_ERROR" in capsys.readouterr().err


def test_restart_resumes_original_steps_and_saved_snapshot_cursor_without_promoting_quality(
    partition_recording, settings
):
    schedules, config, definition, clock, _ = setup(partition_recording, settings)
    assert schedules.select(CONTEXT, definition, status_only=True) is None
    for _ in range(2):
        schedules = CoverageScheduleService(
            schedules.sessions, enabled=True, clock=lambda: clock["now"]
        )
        step = run_tick(config, schedules, clock=schedules.clock)
        assert not step["needs_attention"]
        assert schedules.select(CONTEXT, definition, status_only=True).next_week == date(
            2026, 11, 16
        )
        clock["now"] += timedelta(seconds=1)
    result = drain(schedules, config)
    assert result["coverage"] == "partial" and result["recorded_microseconds"] == 135000000
    assert result["total_usage_microseconds"] is None and not result["current_quota_authorization"]
    status = schedules.select(CONTEXT, definition, status_only=True)
    assert status.next_week == date(2026, 11, 23) and status.revision == 2
    assert run_tick(config, schedules, clock=schedules.clock)["plans"][0]["state"] == "caught_up"
    assert (
        schedules.complete(
            CONTEXT,
            definition,
            date(2026, 11, 16),
            finite(schedules, definition, date(2026, 11, 16)).service,
        ).model_dump(mode="json")
        == result
    )
    clock["now"] = datetime(2026, 11, 30, 1, tzinfo=UTC)
    later = drain(schedules, config)
    assert later["coverage"] in ("partial", "unknown") and later["total_usage_microseconds"] is None
    assert schedules.select(CONTEXT, definition, status_only=True).next_week == date(2026, 11, 30)
    with schedules.sessions() as db:
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 2


def test_closed_days_survive_restart_and_feed_weekly_report_without_raw_samples(
    partition_recording, settings, monkeypatch
):
    schedules, config, legacy, _, _ = setup(partition_recording, settings)
    definition = CoverageScheduleDefinition.model_validate(
        {**legacy.model_dump(mode="json"), "daily_rollups": True}
    )
    assert definition.id != legacy.id and "daily_rollups" not in legacy.model_dump()
    config = config.model_copy(update={"plans": (definition,)})
    week = definition.calendar.first_week
    targets = daily_coverage_targets(definition, week)
    assert [target.day for target in targets] == [
        week + timedelta(days=offset) for offset in range(7)
    ]
    initial = schedules.select(CONTEXT, definition)
    before_due = run_tick(
        config,
        schedules,
        status_only=True,
        clock=lambda: targets[0].scheduled_for - timedelta(seconds=1),
    )["plans"][0]
    assert before_due["state"] == "future_day" and before_due["saved_days"] == 0
    with pytest.raises(DomainError) as missing:
        schedules.complete_daily(CONTEXT, definition, week)
    assert missing.value.code == "COVERAGE_BUILD_PENDING"
    saved = []
    for _ in range(64):
        tick = run_tick(config, schedules, clock=schedules.clock)
        assert not tick["needs_attention"], tick
        progress = tick["plans"][0]
        if progress["state"] == "day_saved":
            saved.append(progress["day"])
            schedules = CoverageScheduleService(
                schedules.sessions, enabled=True, clock=schedules.clock
            )
        if len(saved) == len(targets):
            break
    else:
        pytest.fail("Daily coverage did not converge")
    assert saved == [target.day.isoformat() for target in targets]
    assert schedules.select(CONTEXT, definition, status_only=True) == initial

    complete = CoverageScheduleRepository.complete_daily

    def fail_after_link(self, *args):
        complete(self, *args)
        self.session.flush()
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Injected failure after saving report", 503)

    def require_saved_data(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        assert "agent_messages" not in sql and "usage_window_snapshots" not in sql

    engine = schedules.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", require_saved_data)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(CoverageScheduleRepository, "complete_daily", fail_after_link)
            failed = run_tick(config, schedules, clock=schedules.clock)
            assert failed["needs_attention"]
        assert schedules.select(CONTEXT, definition, status_only=True) == initial
        with schedules.sessions() as db:
            assert db.scalar(select(func.count()).select_from(DailyCoveragePeriodResultRow)) == 0
        step = run_tick(config, schedules, clock=schedules.clock)["plans"][0]
        assert step["state"] == "daily_report_saved"
        output = step["result"]
        assert schedules.complete_daily(CONTEXT, definition, week).model_dump(mode="json") == output
    finally:
        event.remove(engine, "before_cursor_execute", require_saved_data)

    report = output["report"]
    assert [source["snapshot_id"] for source in report["days"]] == [
        str(target.snapshot_id) for target in targets
    ]
    assert report["coverage"] == "partial"
    assert report["recorded_microseconds"] == 135000000
    assert report["total_usage_microseconds"] is None
    assert not report["missing_days"]

    final = schedules.select(CONTEXT, definition, status_only=True)
    assert final.revision == 2 and final.next_week == week + timedelta(days=7)
    assert str(final.last_result_id) == output["id"]
    actor = ActorContext(actor_id=definition.calendar.owner_id, request_id="closed-week")
    with schedules.sessions() as db:
        assert db.scalar(select(func.count()).select_from(DailyCoveragePeriodResultRow)) == 1
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 0
        plan = (
            CoverageScheduleReader(schedules.sessions, (definition.id,), clock=schedules.clock)
            .read_in_transaction(db, actor, node_id="node-a", mode="node")
            .items[0]
        )
        assert str(plan.last_result.report_id) == output["id"]
        assert plan.last_result.snapshot_id is None


def test_daily_weekly_exports_read_the_saved_report_and_respect_member_scope(
    partition_recording, settings
):
    schedules, config, legacy, _, _ = setup(partition_recording, settings)
    definition = CoverageScheduleDefinition.model_validate(
        {**legacy.model_dump(mode="json"), "daily_rollups": True, "session_occupancy": True}
    )
    config = config.model_copy(update={"plans": (definition,)})
    week = definition.calendar.first_week
    ongoing = preview_week_retention(schedules, definition, week + timedelta(days=7))
    assert ongoing.blockers == ("week_still_open",) and ongoing.observations is None
    unbuilt = preview_week_retention(schedules, definition, week)
    assert unbuilt.blockers == ("week_report_missing",) and not unbuilt.deletion_enabled
    for _ in range(64):
        tick = run_tick(config, schedules, clock=schedules.clock)
        assert not tick["needs_attention"], tick
        step = tick["plans"][0]
        if step["state"] == "daily_report_saved":
            break
    else:
        pytest.fail("Daily weekly result was not saved")
    result = step["result"]
    sessions = result["report"]["session_evidence"]
    assert sessions["assessed_days"] == 7 and sessions["retained_microseconds"] == 0
    with schedules.sessions() as db:
        source = db.scalar(
            select(AgentMessage)
            .where(
                AgentMessage.node_id == definition.calendar.scope.id,
                AgentMessage.seq > definition.configuration.trace.initial_after_seq,
                AgentMessage.message_kind == "telemetry",
            )
            .order_by(AgentMessage.seq)
            .limit(1)
        )
        assert source is not None
        day, observation = source.observed_at.date(), source.id
    inspected = verify_daily_coverage(schedules, definition, day, transaction_timeout_ms=30000)
    assert inspected["original_evidence_reverified"] and not inspected["deletion_enabled"]
    assert inspected["report_fingerprint"] == result["fingerprint"]
    assert len(schedules.evidence_review(definition, week).pending_days) == 7
    assert inspected["checked_at"] is None
    checked = verify_daily_coverage(
        schedules, definition, day, transaction_timeout_ms=30000, record=True
    )
    review = schedules.evidence_review(definition, week)
    assert len(review.pending_days) == 6 and not review.deletion_enabled
    checked_statements = []

    def retention_reads(conn, cursor, statement, parameters, context, executemany):
        checked_statements.append(statement)

    with schedules.sessions() as db:
        engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", retention_reads)
    try:
        retirement = preview_week_retention(schedules, definition, week)
    finally:
        event.remove(engine, "before_cursor_execute", retention_reads)
    assert retirement.reviewed == review and not retirement.deletion_enabled
    assert retirement.observations.messages > 0
    assert definition.configuration.trace.consumer_id in {
        consumer.consumer_id for consumer in retirement.known_consumers
    }
    assert "partition-recording-fixture" in {
        consumer.consumer_id for consumer in retirement.known_consumers
    }
    assert "unsatisfied_daily_evidence_checks" in retirement.blockers
    assert retirement.payload_prefix is None
    assert "future_report_after_retirement" in retirement.remaining_checks
    assert len(retirement.daily_arrivals) == 7
    selected_day = next(value for value in retirement.daily_arrivals if value.day == day)
    assert selected_day.outside_freeze == 0 and selected_day.archive_read_at is not None
    for statement in checked_statements:
        assert statement.lstrip().split(maxsplit=1)[0].upper() in ("BEGIN", "SELECT", "SET")
        assert "agent_messages.payload" not in statement.lower()
    statements_by_day = [sql.lower() for sql in checked_statements if "day_ordinal" in sql.lower()]
    assert len(statements_by_day) == 1
    assert statements_by_day[0].count("union all") == 6
    assert retirement.coverage_builds is not None
    assert retirement.coverage_builds.builds_without_saved_windows == 0
    assert not retirement.coverage_builds.truncated
    for unchecked in review.pending_days:
        verify_daily_coverage(
            schedules, definition, unchecked, transaction_timeout_ms=30000, record=True
        )
    review = schedules.evidence_review(definition, week)
    assert not review.pending_days
    checked_statements.clear()
    event.listen(engine, "before_cursor_execute", retention_reads)
    try:
        checked_retirement = preview_week_retention(schedules, definition, week)
    finally:
        event.remove(engine, "before_cursor_execute", retention_reads)
    assert checked_retirement.payload_prefix is not None, checked_retirement.blockers
    assert not checked_retirement.deletion_enabled
    assert any(
        "order by agent_messages.seq" in statement.lower() for statement in checked_statements
    )
    assert not any(
        "agent_messages.payload" in statement.lower() for statement in checked_statements
    )
    draft = build_lyra_coverage_components(
        schedules.sessions,
        LyraCoverageRuntimeConfig(
            enabled=True,
            configuration=definition.configuration,
            target=CoverageBuildTarget(
                id=uuid4(), query=daily_coverage_targets(definition, week)[0].query
            ),
            transaction_timeout_ms=30000,
        ),
        clock=schedules.clock,
    )
    assert draft.run_once()["state"] == "admitted"
    with_draft = preview_week_retention(schedules, definition, week)
    assert (
        with_draft.coverage_builds.reviewed_builds == retirement.coverage_builds.reviewed_builds + 1
    )
    assert with_draft.coverage_builds.builds_without_saved_windows == 1
    assert "coverage_builds_without_saved_windows" in with_draft.blockers
    assert with_draft.payload_prefix is None
    check = next(value for value in review.checked_days if value.day == day)
    assert check.day == day and check.checked_at.isoformat() == checked["checked_at"]
    evidence = DailyCoverageEvidence(**check.model_dump(exclude={"checked_at"}))
    assert schedules.save_evidence_check(definition, evidence) == check
    with pytest.raises(DomainError) as error:
        schedules.save_evidence_check(
            definition, evidence.model_copy(update={"snapshot_fingerprint": "0" * 64})
        )
    assert error.value.code == "CONFLICT"
    with schedules.sessions.begin() as db:
        db.execute(
            update(AgentMessage)
            .where(AgentMessage.id == observation)
            .values(received_at=retirement.end_at)
        )
    late_retirement = preview_week_retention(schedules, definition, week)
    assert late_retirement.observations.messages == retirement.observations.messages
    assert late_retirement.observations.late_arrivals == retirement.observations.late_arrivals + 1
    assert "late_arrivals_need_review" in late_retirement.blockers
    with schedules.sessions.begin() as db:
        db.execute(
            update(AgentMessage)
            .where(AgentMessage.id == observation)
            .values(received_at=selected_day.archive_read_at + timedelta(microseconds=1))
        )
    outside = preview_week_retention(schedules, definition, week)
    changed_day = next(value for value in outside.daily_arrivals if value.day == day)
    assert changed_day.outside_freeze == 1
    assert "packets_outside_daily_freeze" in outside.blockers
    assert outside.reviewed == review and not outside.deletion_enabled
    # Simulate expired evidence in this test database only.
    with schedules.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.id == observation))
    with pytest.raises(DomainError):
        verify_daily_coverage(schedules, definition, day, transaction_timeout_ms=30000, record=True)
    assert schedules.evidence_review(definition, week) == review

    def no_reconstruction(conn, cursor, statement, parameters, context, executemany):
        assert not any(
            table in statement.lower()
            for table in ("agent_messages", "usage_ledger_entries", "usage_coverage_manifests")
        )

    timeline_engine = schedules.sessions.kw["bind"]
    event.listen(timeline_engine, "before_cursor_execute", no_reconstruction)
    try:
        timeline = schedules.saved_day_timeline(definition, day)
    finally:
        event.remove(timeline_engine, "before_cursor_execute", no_reconstruction)
    assert timeline.reference.day == day
    assert timeline.reference.rollup_fingerprint == check.rollup_fingerprint
    assert timeline.reference.snapshot_fingerprint == check.snapshot_fingerprint
    assert timeline.verified_short_gaps == ()
    assert (
        sum(cell.microseconds for cell in timeline.intervals if cell.state == "recorded")
        == timeline.verified_usage_microseconds
    )
    assert any(cell.state in {"unknown", "uncovered"} for cell in timeline.intervals)

    owner = str(definition.calendar.owner_id)
    member = str(partition_recording[2][2][2][4][1])
    role = str(uuid4())
    with schedules.sessions.begin() as db:
        permission = db.scalar(select(Permission.id).where(Permission.name == "usage.read_own"))
        assert permission
        db.add(Role(id=role, name="fixture-weekly-member"))
        db.flush()
        db.add(RolePermission(role_id=role, permission_id=permission))
        db.add(
            RoleBinding(
                id=str(uuid4()), user_id=member, role_id=role, scope_type="node", scope_id="node-a"
            )
        )
        members = {
            identity.user_id: ExternalSubject(
                provider=identity.provider,
                namespace=identity.namespace,
                external_id=identity.external_id,
            )
            for identity in db.scalars(
                select(ExternalIdentity).where(ExternalIdentity.user_id.in_((owner, member)))
            )
        }
    token_admin, token_member = str(uuid4()), str(uuid4())
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(subject=members[owner], token=SecretStr(token_admin)),
            UserTokenBinding(subject=members[member], token=SecretStr(token_member)),
        ],
        reserved_tokens=[settings.read_token],
    )
    surface = UsageHistorySurface(
        UsageHistoryService(schedules.sessions, clock=schedules.clock),
        auth,
        ("node-a",),
        saved_windows=UsageWindowReader(schedules.sessions),
        coverage_plans=CoverageScheduleReader(
            schedules.sessions, (definition.id,), include_node_for_personal=True
        ),
    )
    app = create_app(
        settings,
        SlackSettings(_env_file=None, signing_secret="", bot_token="", team_id=""),
        usage_history=surface,
    )

    def only_report(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        for table in ("agent_messages", "usage_daily_rollups", "usage_window_snapshots"):
            assert table not in sql

    engine = schedules.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", only_report)
    payload = {"id": result["id"], "source": "daily_rollups", "node_id": "node-a"}
    try:
        combined = schedules.combined_week((definition,), week)
        assert (
            combined.verified_usage_microseconds == result["report"]["verified_usage_microseconds"]
        )
        original = DailyCoveragePeriodResult.model_validate(result)
        partial = combine_coverage_week(
            week, definition.calendar.timezone, {"node-b": None, "node-a": original}
        )
        assert partial.verified_usage_microseconds == combined.verified_usage_microseconds
        assert partial.total_usage_microseconds is None
        assert partial.nodes[-1].coverage == "missing"
        assert partial.nodes[-1].verified_usage_microseconds is None
        assert partial == combine_coverage_week(
            week, definition.calendar.timezone, {"node-a": original, "node-b": None}
        )
        combined_rows = list(
            csv.DictReader(io.StringIO(combined_week_csv(partial).lstrip("\ufeff")))
        )
        assert len(combined_rows) == 3
        assert combined_rows[0]["scope"] == "lab"
        assert combined_rows[0]["total_usage_microseconds"] == ""
        assert combined_rows[1]["node_report_fingerprint"] == original.fingerprint
        assert combined_rows[2]["coverage"] == "missing"
        assert combined_rows[2]["verified_usage_microseconds"] == ""
        assert all(row["combined_fingerprint"] == partial.fingerprint for row in combined_rows)
        assert all(None not in row and None not in row.values() for row in combined_rows)
        combined_html = combined_week_html(partial)
        assert partial.fingerprint in combined_html
        assert "缺少周报" in combined_html and "总用量 未知" in combined_html
        assert owner not in combined_html and "<script" not in combined_html
        with TestClient(app) as client:
            for route in ("read", "report", "csv"):
                path = "/internal/usage/weekly/" + route
                admin = {"Authorization": f"Bearer {token_admin}"}
                regular = {"Authorization": f"Bearer {token_member}"}
                visible = client.post(path, headers=admin, json={**payload, "mode": "node"})
                hidden = client.post(path, headers=regular, json={**payload, "mode": "mine"})
                denied = client.post(path, headers=regular, json={**payload, "mode": "node"})
                assert visible.status_code == hidden.status_code == 200
                assert "no-store" in hidden.headers["cache-control"]
                assert denied.status_code == 403
                assert owner not in hidden.text
                if route == "read":
                    assert visible.json()["verified_usage_microseconds"] > 0
                    assert (
                        visible.json()["verified_session_microseconds"]
                        == sessions["verified_session_microseconds"]
                    )
                    assert hidden.json()["durations"] == []
                    assert hidden.json()["verified_session_microseconds"] == 0
                    assert hidden.json()["total_usage_microseconds"] is None
                    assert len(hidden.json()["days"]) == 7
                elif route == "report":
                    assert "GPU 用量周报" in visible.text
                    assert "已核实会话占用" in visible.text
                else:
                    rows = list(csv.DictReader(io.StringIO(visible.content.decode("utf-8-sig"))))
                    assert any(row["owner_id"] == owner for row in rows)
                    assert all(None not in row and None not in row.values() for row in rows)
        with schedules.sessions.begin() as db:
            db.execute(
                delete(RolePermission).where(
                    RolePermission.permission_id.in_(
                        select(Permission.id).where(Permission.name == "usage.read_all")
                    )
                )
            )
        with pytest.raises(DomainError) as refused:
            schedules.combined_week((definition,), week)
        assert refused.value.code == "PERMISSION_DENIED"
        with pytest.raises(DomainError) as refused_timeline:
            schedules.saved_day_timeline(definition, day)
        assert refused_timeline.value.code == "PERMISSION_DENIED"
    finally:
        event.remove(engine, "before_cursor_execute", only_report)


def test_day_revision_uses_distinct_saved_rollup_and_preserves_published_week(
    partition_recording, settings
):
    schedules, config, legacy, _, graph = setup(partition_recording, settings)
    definition = CoverageScheduleDefinition.model_validate(
        {**legacy.model_dump(mode="json"), "daily_rollups": True}
    )
    config = config.model_copy(update={"plans": (definition,)})
    for _ in range(64):
        outcome = run_tick(config, schedules, clock=schedules.clock)
        assert not outcome["needs_attention"]
        if outcome["plans"][0]["state"] == "daily_report_saved":
            break
    else:
        pytest.fail("Original daily report was not published")

    week = definition.calendar.first_week
    old = schedules.verify_daily_week(CONTEXT, definition, week)
    old_review = schedules.evidence_review(definition, week)
    day = week
    evening = old.report.start_at + timedelta(hours=19)
    with schedules.sessions() as db:
        last = db.scalar(select(func.max(AgentMessage.seq)))
    client = partition_recording[2][4]
    for offset in range(4):
        sample = resource_batch(
            seq=last + offset + 1, observed_at=evening + timedelta(seconds=15 * offset)
        )
        if offset < 3:
            ownership(sample, [owner(username="linux-user")])
        else:
            sample["resources"]["compute"].update(reported_processes=0, gpus=[])
        assert put(client, sample).status_code == 200
    for _ in range(8):
        assert not graph.run_once()["failures"]
    held = preview_week_retention(schedules, definition, week)
    assert "packets_outside_daily_freeze" in held.blockers
    request = uuid4()
    paused = CoverageScheduleService(schedules.sessions, clock=schedules.clock)

    def prepare(service):
        return prepare_day_revision(
            service,
            definition,
            day,
            request,
            prefix_proofs_per_tick=8,
            transaction_timeout_ms=30000,
        )

    with pytest.raises(DomainError) as error:
        prepare(paused)
    assert error.value.code == "WEEKLY_COVERAGE_NOT_CONFIGURED"
    for _ in range(24):
        candidate = prepare(schedules)
        assert not candidate["published"]
        if candidate["state"] == "revision_ready":
            break
    else:
        pytest.fail("Replacement daily rollup was not saved")
    proposed = candidate["candidate"]
    assert candidate["official_report_fingerprint"] == old.fingerprint
    assert proposed["fingerprint"] != old.report.fingerprint
    assert candidate["comparison"]["verified_usage_microseconds_delta"] == 30_000_000
    assert [
        source["day"]
        for source, official_day in zip(proposed["days"], old.report.days, strict=True)
        if source["snapshot_id"] != str(official_day.snapshot_id)
    ] == [day.isoformat()]
    assert prepare(paused) == candidate
    assert schedules.verify_daily_week(CONTEXT, definition, week) == old
    assert schedules.evidence_review(definition, week) == old_review


def test_snapshot_saved_before_cursor_failure_is_recovered_atomically_and_retained(
    partition_recording, settings, monkeypatch
):
    schedules, config, definition, _, _ = setup(partition_recording, settings)
    original = CoverageScheduleRepository.complete

    def fail_after_link(self, *args):
        original(self, *args)
        self.session.flush()
        raise DomainError("DEPENDENCY_UNAVAILABLE", "synthetic failure after result link", 503)

    with monkeypatch.context() as patch:
        patch.setattr(CoverageScheduleRepository, "complete", fail_after_link)
        for _ in range(12):
            step = run_tick(config, schedules, clock=schedules.clock)
            if step["needs_attention"]:
                break
        assert step["plans"][0]["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert (
        schedules.select(CONTEXT, definition, status_only=True).next_week
        == definition.calendar.first_week
    )
    with schedules.sessions() as db:
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 0
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM domain_events "
                    "WHERE event_type='weekly_coverage.snapshot_saved'"
                )
            )
            == 0
        )
    result = drain(schedules, config)
    for statement in (
        update(CoveragePeriodResultRow).values(fingerprint="f" * 64),
        delete(CoveragePeriodResultRow),
        update(CoverageScheduleRow).values(fingerprint="f" * 64),
        delete(CoverageScheduleRow),
    ):
        with pytest.raises(DBAPIError), schedules.sessions.begin() as db:
            db.execute(statement)
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0042")
    assert (
        str(schedules.select(CONTEXT, definition, status_only=True).last_result_id) == result["id"]
    )


def test_same_schedule_changed_inputs_skipped_period_and_current_revocation_are_rejected(
    partition_recording, settings
):
    schedules, config, definition, clock, _ = setup(partition_recording, settings)
    schedules.select(CONTEXT, definition)
    changed = definition.model_copy(
        update={"calendar": definition.calendar.model_copy(update={"timezone": "Europe/London"})}
    )
    assert changed.id == definition.id
    with pytest.raises(DomainError, match="different input"):
        schedules.select(CONTEXT, changed)
    clock["now"] = datetime(2026, 12, 7, 1, tzinfo=UTC)
    with pytest.raises(DomainError, match="cannot skip"):
        schedules.complete(
            CONTEXT,
            definition,
            date(2026, 11, 23),
            finite(schedules, definition, date(2026, 11, 23)).service,
        )
    with schedules.sessions.begin() as db:
        db.execute(
            delete(RoleBinding).where(RoleBinding.user_id == str(definition.calendar.owner_id))
        )
    value = run_tick(config, schedules, clock=schedules.clock)
    assert value["needs_attention"] and value["plans"][0]["error_code"] == "PERMISSION_DENIED"
    with schedules.sessions() as db:
        assert db.scalar(select(CoverageScheduleRow.next_week)) == date(2026, 11, 16)
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 0


def test_uncaught_source_waits_before_admission_then_resumes_the_same_week(
    partition_recording, settings
):
    schedules, config, definition, _, graph = setup(partition_recording, settings)
    assert beat(partition_recording[2][4], heartbeat(seq=25)).status_code == 200
    for _ in range(4):
        step = run_tick(config, schedules, clock=schedules.clock)
        if step["plans"][0]["state"] == "waiting":
            break
    assert step["plans"][0]["state"] == "waiting" and not step["needs_attention"]
    assert schedules.select(CONTEXT, definition, status_only=True).next_week == date(2026, 11, 16)
    for _ in range(3):
        assert not graph.run_once()["failures"]
    assert drain(schedules, config)["coverage"] == "partial"


def test_postgres_duplicate_completion_is_one_result_and_public_evidence_is_read_only(
    partition_recording, settings
):
    schedules, _, definition, _, _ = setup(partition_recording, settings)
    if schedules.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL coordinator locking and READ ONLY")
    schedules.select(CONTEXT, definition)
    graph = finite(schedules, definition, definition.calendar.first_week)
    for _ in range(12):
        if graph.run_once()["state"] == "finished":
            break
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: schedules.complete(
                    CONTEXT, definition, definition.calendar.first_week, graph.service
                ),
                range(2),
            )
        )
    assert results[0] == results[1]
    target = weekly_coverage_target(
        definition.calendar,
        weekly_report_period(definition.calendar, definition.calendar.first_week),
        definition.configuration,
        now=schedules._now(),
    )
    with schedules.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        assert (
            graph.service.completed_in_transaction(db, target).request.id == results[0].snapshot_id
        )
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 1
        assert db.scalar(select(CoverageScheduleRow.revision)) == 2


def test_empty_report_migrations_roundtrip_and_metadata_drift(database_url, monkeypatch):
    monkeypatch.setenv("ARIS_DATABASE_URL", database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0042")
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, "0042")
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_configured_status_tracks_due_dates_saved_quality_and_personal_scope_without_writing(
    partition_recording, settings
):
    schedules, config, definition, clock, _ = setup(partition_recording, settings)
    actor = ActorContext(actor_id=definition.calendar.owner_id, request_id="fixture-status")
    schedules.select(CONTEXT, definition)
    personal = definition.model_copy(
        update={"calendar": definition.calendar.model_copy(update={"report_type": "usage.mine"})}
    )
    schedules.select(CONTEXT, personal)
    other = UUID(partition_recording[2][2][2][4][1])
    with schedules.sessions.begin() as db:
        role = db.scalar(
            select(Role.id)
            .join(RoleBinding, RoleBinding.role_id == Role.id)
            .where(
                RoleBinding.user_id == str(actor.actor_id),
                Role.name.like("fixture-ledger-reader-%"),
            )
        )
        db.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(other),
                role_id=role,
                scope_type="node",
                scope_id="node-a",
            )
        )
    other_plan = personal.model_copy(
        update={"calendar": personal.calendar.model_copy(update={"owner_id": other})}
    )
    schedules.select(CONTEXT, other_plan)
    reader = CoverageScheduleReader(
        schedules.sessions,
        (definition.id, personal.id, other_plan.id, uuid4()),
        clock=schedules.clock,
    )

    def read(mode):
        with schedules.sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
            return reader.read_in_transaction(db, actor, node_id="node-a", mode=mode)

    assert [item.id for item in read("mine").items] == [personal.id]
    reader.include_node_for_personal = True
    assert {item.id for item in read("mine").items} == {definition.id, personal.id}
    assert other_plan.id not in {item.id for item in read("mine").items}
    assert [item.id for item in read("node").items] == [definition.id]
    assert read("node").items[0].state == "delayed"
    saved = drain(schedules, config)
    item = read("node").items[0]
    assert item.state == "scheduled" and item.next_week == date(2026, 11, 23)
    assert str(item.last_result.snapshot_id) == saved["snapshot_id"]
    assert item.last_result.coverage == "partial" and item.worker_health == "not_assessed"
    clock["now"] = item.next_due_at + timedelta(minutes=1)
    assert read("node").items[0].state == "pending"
    clock["now"] += timedelta(minutes=30)
    assert read("node").items[0].state == "delayed"
    raw = item.model_dump_json()
    assert "configuration" not in raw and "agent_id" not in raw and "owner_id" not in raw
    with schedules.sessions() as db:
        assert db.scalar(select(func.count()).select_from(CoverageScheduleRow)) == 3
        assert db.scalar(select(func.count()).select_from(CoveragePeriodResultRow)) == 1
    with schedules.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
    with pytest.raises(DomainError) as rejected:
        read("mine")
    assert rejected.value.code == "PERMISSION_DENIED"
