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
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.apps.composition.lyra import build_lyra_components
from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.apps.ops.weekly_coverage import WeeklyCoverageConfig, main, run_tick
from aris.core.access.models import Role, RoleBinding
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.scheduler.schemas import SchedulerContext
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.nova.coverage_schedule_models import CoveragePeriodResultRow, CoverageScheduleRow
from aris.modules.nova.coverage_schedule_reader import CoverageScheduleReader
from aris.modules.nova.coverage_schedule_repo import CoverageScheduleRepository
from aris.modules.nova.coverage_schedule_schemas import CoverageScheduleDefinition
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.daily_coverage_calendar import daily_coverage_targets
from aris.modules.nova.report_calendar import WeeklyReportPlan, weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target
from tests.test_heartbeat import beat, heartbeat
from tests.test_recorded_usage import reading_service
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
    partition_recording, settings
):
    schedules, config, definition, _, _ = setup(partition_recording, settings)
    week = definition.calendar.first_week
    targets = daily_coverage_targets(definition, week)
    assert [target.day for target in targets] == [
        week + timedelta(days=offset) for offset in range(7)
    ]
    before_due = run_tick(
        config,
        schedules,
        from_daily_week=week,
        status_only=True,
        clock=lambda: targets[0].scheduled_for - timedelta(seconds=1),
    )["plans"][0]
    assert before_due["state"] == "future_day" and before_due["saved_days"] == 0
    saved = []
    for _ in range(64):
        tick = run_tick(config, schedules, from_daily_week=week, clock=schedules.clock)
        assert not tick["needs_attention"], tick
        progress = tick["plans"][0]
        if progress["state"] == "day_saved":
            saved.append(progress["day"])
            schedules = CoverageScheduleService(
                schedules.sessions, enabled=True, clock=schedules.clock
            )
        elif progress["state"] == "report_ready":
            break
    else:
        pytest.fail("Daily coverage did not converge")
    assert saved == [target.day.isoformat() for target in targets]
    report = progress["report"]
    assert [source["snapshot_id"] for source in report["days"]] == [
        str(target.snapshot_id) for target in targets
    ]
    assert report["coverage"] == "partial"
    assert report["recorded_microseconds"] == 135000000
    assert report["total_usage_microseconds"] is None
    assert not report["missing_days"]

    def require_saved_data(conn, cursor, statement, parameters, context, executemany):
        assert "agent_messages" not in statement.lower()
        assert not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))

    engine = schedules.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", require_saved_data)
    try:
        repeat = run_tick(config, schedules, from_daily_week=week, status_only=True)
    finally:
        event.remove(engine, "before_cursor_execute", require_saved_data)
    assert repeat["plans"][0] == progress
    assert schedules.select(CONTEXT, definition, status_only=True) is None


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


def test_empty_schedule_migration_roundtrip_and_metadata_drift(database_url, monkeypatch):
    monkeypatch.setenv("ARIS_DATABASE_URL", database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0042")
    command.upgrade(cfg, "0043")
    command.check(cfg)
    command.downgrade(cfg, "0042")
    command.upgrade(cfg, "0043")
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
