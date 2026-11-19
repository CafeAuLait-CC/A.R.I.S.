import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import RoleBinding
from aris.core.access.schemas import AccessScope
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.core.scheduler.models import ScheduledJobRow
from aris.core.scheduler.schemas import SchedulerContext, SchedulerLimits
from aris.core.scheduler.service import SchedulerService
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_models import WeeklyReportRow
from aris.modules.lyra.weekly_report_service import WeeklyReportHandler
from aris.modules.nova.report_calendar import WeeklyReportPlan
from aris.modules.nova.weekly_plan_models import WeeklyPlanRow
from aris.modules.nova.weekly_plan_reader import WeeklyPlanReader
from aris.modules.nova.weekly_plan_repo import WeeklyPlanRepository
from aris.modules.nova.weekly_plan_schemas import WeeklyPlanClaim, WeeklyPreparationLimits, plan_id
from aris.modules.nova.weekly_plan_service import WeeklyPlanService
from tests.test_scheduler import scheduled

__all__ = ["scheduled"]
CONTEXT = SchedulerContext(request_id="fixture-persistent-weekly-plan")


@pytest.fixture
def plans(scheduled):
    _, request, _, sessions, clock, _ = scheduled
    history = UsageHistoryService(sessions, clock=lambda: clock[0])
    handler = WeeklyReportHandler(history)
    worker = SchedulerService(
        sessions,
        tuple((definition, handler) for definition in handler.definitions()),
        limits=SchedulerLimits(),
        clock=lambda: clock[0],
    )
    plan = WeeklyReportPlan(
        namespace="fixture",
        report_type="usage.mine",
        owner_id=request.owner_id,
        scope=AccessScope(type="node", id="node-a"),
        timezone="UTC",
        first_week=date(2026, 8, 24),
    )

    def service():
        return WeeklyPlanService(
            sessions, worker, limits=WeeklyPreparationLimits(), clock=lambda: clock[0]
        )

    return service, plan, worker, sessions, clock


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_unconfigured_weekly_preparation_does_not_open_database():
    def no_database():
        pytest.fail("disabled weekly preparation accessed database")

    service = WeeklyPlanService(no_database, SchedulerService(no_database))
    assert service.run_once(CONTEXT, None).state == "idle"


def test_more_than_eight_weeks_resume_after_restart_and_new_weeks_arrive(plans):
    factory, plan, worker, sessions, clock = plans
    first = factory().run_once(CONTEXT, plan)
    assert first.state == "prepared" and len(first.task_ids) == 8 and not first.caught_up
    second = factory().run_once(CONTEXT, plan)
    assert second.state == "prepared" and len(second.task_ids) == 4 and second.caught_up
    assert second.next_week == date(2026, 11, 16)
    before = count(sessions, DomainEvent)
    assert factory().run_once(CONTEXT, plan).state == "idle"
    assert count(sessions, DomainEvent) == before
    assert count(sessions, ScheduledJobRow) == 12
    assert count(sessions, WeeklyReportRow) == 0  # Prepared is not completed.
    clock[0] = datetime(2026, 11, 23, tzinfo=UTC)
    third = factory().run_once(CONTEXT, plan)
    assert len(third.task_ids) == 1 and third.next_week == date(2026, 11, 23)
    assert count(sessions, ScheduledJobRow) == 13
    assert worker.run_once(CONTEXT).state == "completed"
    assert count(sessions, WeeklyReportRow) == 1


def test_partial_enqueue_failure_retains_cursor_and_reuses_committed_task(plans, monkeypatch):
    factory, plan, worker, sessions, clock = plans
    original = worker.enqueue
    calls = [0]

    def fail_second(*args):
        calls[0] += 1
        if calls[0] == 2:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Synthetic interruption", 503)
        return original(*args)

    monkeypatch.setattr(worker, "enqueue", fail_second)
    result = factory().run_once(CONTEXT, plan)
    assert result.state == "retry" and count(sessions, ScheduledJobRow) == 1
    stored = factory().exact(plan_id(plan))
    assert stored.next_week == plan.first_week and stored.revision == 2
    assert factory().run_once(CONTEXT, plan).state == "idle"
    clock[0] = stored.retry_at
    monkeypatch.setattr(worker, "enqueue", original)
    recovered = factory().run_once(CONTEXT, plan)
    assert recovered.state == "prepared" and len(recovered.task_ids) == 8
    assert count(sessions, ScheduledJobRow) == 8
    assert factory().exact(plan_id(plan)).last_error_code is None


def test_failure_after_all_tasks_does_not_advance_progress(plans, monkeypatch):
    factory, plan, _, sessions, clock = plans
    original = WeeklyPlanRepository.update
    failed = [False]

    def fail_progress(self, id, **values):
        if "next_week" in values and not failed[0]:
            failed[0] = True
            raise RuntimeError("Synthetic process failure before progress")
        return original(self, id, **values)

    monkeypatch.setattr(WeeklyPlanRepository, "update", fail_progress)
    assert factory().run_once(CONTEXT, plan).state == "retry"
    assert count(sessions, ScheduledJobRow) == 8
    stored = factory().exact(plan_id(plan))
    assert stored.next_week == plan.first_week
    clock[0] = stored.retry_at
    assert factory().run_once(CONTEXT, plan).state == "prepared"
    assert count(sessions, ScheduledJobRow) == 8


def test_expired_preparer_cannot_overwrite_replacement_cursor(plans):
    factory, plan, _, sessions, clock = plans
    first = factory().claim(CONTEXT, plan)
    assert isinstance(first, WeeklyPlanClaim)
    assert factory().claim(CONTEXT, plan).state == "busy"
    clock[0] = first.expires_at
    second = factory().claim(CONTEXT, plan)
    assert isinstance(second, WeeklyPlanClaim) and second.lease_token != first.lease_token
    assert factory().prepare_claim(CONTEXT, second).state == "prepared"
    assert factory().prepare_claim(CONTEXT, first).state == "lost_claim"
    assert count(sessions, ScheduledJobRow) == 8


def test_current_revoke_prevents_new_tasks_and_progress(plans):
    factory, plan, _, sessions, _ = plans
    claim = factory().claim(CONTEXT, plan)
    with sessions.begin() as session:
        session.execute(delete(RoleBinding))
    result = factory().prepare_claim(CONTEXT, claim)
    assert result.state == "retry" and result.error_code == "PERMISSION_DENIED"
    assert factory().exact(plan_id(plan)).next_week == plan.first_week
    assert count(sessions, ScheduledJobRow) == 0


def test_changed_plan_definition_conflicts_instead_of_reinterpreting_history(plans):
    factory, plan, _, sessions, _ = plans
    factory().run_once(CONTEXT, plan)
    prior = factory().exact(plan_id(plan))
    with pytest.raises(DomainError) as changed:
        factory().run_once(CONTEXT, plan.model_copy(update={"timezone": "Asia/Tokyo"}))
    assert changed.value.code == "CONFLICT"
    assert factory().exact(plan_id(plan)) == prior
    assert count(sessions, WeeklyPlanRow) == 1


def test_future_plan_is_saved_without_scheduling_unfinished_week(plans):
    factory, plan, _, sessions, _ = plans
    future = plan.model_copy(update={"first_week": date(2026, 11, 16)})
    result = factory().run_once(CONTEXT, future)
    assert result.state == "idle" and result.caught_up
    assert result.next_due_at == datetime(2026, 11, 23, tzinfo=UTC)
    assert count(sessions, WeeklyPlanRow) == 1 and count(sessions, ScheduledJobRow) == 0


def test_plan_definition_progress_and_nonempty_history_are_protected(plans):
    factory, plan, _, sessions, _ = plans
    factory().run_once(CONTEXT, plan)
    for values in ({"canonical_plan": "{}"}, {"next_week": plan.first_week}):
        with pytest.raises(DBAPIError), sessions.begin() as session:
            session.execute(update(WeeklyPlanRow).values(**values))
    with pytest.raises(RuntimeError, match="weekly plan history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0027")


def test_postgres_preparers_claim_one_plan_once(plans, database_url):
    factory, plan, _, sessions, _ = plans
    if not database_url.startswith("postgresql"):
        pytest.skip("requires PostgreSQL plan locks")
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=5)
        return factory().claim(CONTEXT, plan)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, range(2)))
    claims = [value for value in results if isinstance(value, WeeklyPlanClaim)]
    assert len(claims) == 1 and [
        value.state for value in results if not isinstance(value, WeeklyPlanClaim)
    ] == ["busy"]
    assert factory().prepare_claim(CONTEXT, claims[0]).state == "prepared"
    assert count(sessions, ScheduledJobRow) == 8


def test_real_cli_processes_resume_persistent_weeks_then_run_and_stop(
    plans, database_url, tmp_path
):
    _, plan, _, sessions, _ = plans
    config = tmp_path / "weekly-private.json"
    config.write_text(
        json.dumps(
            {
                "environment": "test",
                "database_url": database_url,
                "plans": [plan.model_dump(mode="json")],
                "poll_seconds": 1,
            }
        )
    )
    config.chmod(0o600)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))
    }
    environment["PYTHONPATH"] = str(Path.cwd() / "src")

    def run(*args):
        result = subprocess.run(
            [sys.executable, "-m", "aris.apps.ops.weekly_reports", "--config", str(config), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return [json.loads(line) for line in result.stdout.splitlines()]

    before = run("--status")[0]
    assert before["plans"][0]["state"] == "not_registered" and count(sessions, WeeklyPlanRow) == 0
    assert run("--once")[0]["plans"][0]["tasks_prepared"] == 8
    assert run("--once")[0]["plans"][0]["tasks_prepared"] == 4
    assert count(sessions, ScheduledJobRow) == count(sessions, WeeklyReportRow) == 12
    status = run("--status")[0]
    assert status["plans"][0]["next_week"] == "2026-11-16"
    assert status["plans"][0]["worker_health"] == "not_assessed"
    loop = run("--run", "--max-seconds", "1")
    assert loop[0]["result"] == "worker_started" and loop[-1]["result"] == "worker_stopped"
    assert count(sessions, ScheduledJobRow) == count(sessions, WeeklyReportRow) == 12


def test_plan_reader_uses_literal_prefix_and_separates_pending_completed_and_overdue(plans):
    factory, plan, worker, sessions, clock = plans
    first = plan.model_copy(update={"namespace": "fixture_"})
    second = plan.model_copy(update={"namespace": "fixtureX"})
    factory().run_once(CONTEXT, first)
    factory().run_once(CONTEXT, second)
    actor = ActorContext(actor_id=plan.owner_id, request_id="fixture-read-plan")
    reader = WeeklyPlanReader(sessions, clock=lambda: clock[0])
    with sessions() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        page = reader.read_in_transaction(session, actor, node_id="node-a")
        assert len(page.items) == 2
        assert all(
            item.jobs.counts["pending"] == 8 and item.jobs.overdue_count == 0 for item in page.items
        )
        assert all(item.worker_health == "not_assessed" for item in page.items)
        assert "lease_token" not in page.model_dump_json()
    clock[0] += timedelta(minutes=31)
    with sessions() as session:
        page = reader.read_in_transaction(session, actor, node_id="node-a")
        assert all(item.jobs.overdue_count == 8 for item in page.items)
    assert worker.run_once(CONTEXT).state == "completed"
    with sessions() as session:
        page = reader.read_in_transaction(session, actor, node_id="node-a")
        assert sum(item.jobs.counts["completed"] for item in page.items) == 1
        assert sum(item.jobs.overdue_count for item in page.items) == 15


def test_plan_reader_checks_current_permissions_after_queue_read(plans, monkeypatch):
    factory, plan, _, sessions, clock = plans
    factory().run_once(CONTEXT, plan)
    actor = ActorContext(actor_id=plan.owner_id, request_id="fixture-read-plan")
    reader = WeeklyPlanReader(sessions, clock=lambda: clock[0])
    with sessions() as session, pytest.raises(DomainError) as denied:
        reader.read_in_transaction(session, actor, node_id="node-a", mode="node")
    assert denied.value.code == "PERMISSION_DENIED"
    original = reader.queue.queue_in_transaction

    def revoke(session, *args, **kwargs):
        result = original(session, *args, **kwargs)
        session.execute(delete(RoleBinding))
        return result

    monkeypatch.setattr(reader.queue, "queue_in_transaction", revoke)
    with sessions() as session, pytest.raises(DomainError) as revoked:
        reader.read_in_transaction(session, actor, node_id="node-a")
    assert revoked.value.code == "PERMISSION_DENIED"
