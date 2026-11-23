import json
from datetime import UTC, date, datetime, timedelta
from functools import partial
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.apps.api.main import create_app
from aris.apps.ops.weekly_reports import main as weekly_main
from aris.core.access.models import RoleBinding
from aris.core.access.schemas import AccessScope
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.core.scheduler.schemas import SchedulerContext, SchedulerLimits
from aris.core.scheduler.service import SchedulerService
from aris.interfaces.slack.config import SlackSettings
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.ledger_segment_schemas import CaptureLedgerSegment, ledger_read_configuration
from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from aris.modules.lyra.usage_history import UsageHistoryQuery
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_models import WeeklyReportRow
from aris.modules.lyra.weekly_report_service import WeeklyReportHandler, WeeklyReportReader
from aris.modules.nova.report_calendar import WeeklyReportPlan
from aris.modules.nova.report_scheduler import enqueue_weekly_reports
from aris.modules.nova.weekly_plan_reader import WeeklyPlanReader
from aris.modules.nova.weekly_plan_schemas import WeeklyPreparationLimits
from aris.modules.nova.weekly_plan_service import WeeklyPlanService
from aris.modules.vega import service as vega_service
from tests import test_telemetry, test_usage_derivations, test_usage_ledger
from tests.test_administration import NOW, admin_state
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_usage_derivations import derivations
from tests.test_usage_history import TOKEN, surface_for
from tests.test_usage_ledger import POST, ledger, post_window

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings"]
CONTEXT = SchedulerContext(request_id="fixture-weekly-report")


@pytest.fixture(autouse=True)
def report_source_clock(monkeypatch):
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            received = NOW + timedelta(seconds=220)
            return received.astimezone(tz) if tz is not None else received.replace(tzinfo=None)

    for module in (vega_service, test_telemetry, test_usage_derivations, test_usage_ledger):
        monkeypatch.setattr(module, "datetime", FixtureClock)


def weekly(readings, mode="mine"):
    state, (original, actor, _, _), _ = readings
    clock = [datetime(2026, 11, 23, 1, tzinfo=UTC)]
    history = IndexedUsageHistoryService(original.sessions, clock=lambda: clock[0])
    handler = WeeklyReportHandler(history)
    scheduler = SchedulerService(
        history.sessions,
        tuple((definition, handler) for definition in handler.definitions()),
        limits=SchedulerLimits(),
        clock=lambda: clock[0],
    )
    plan = WeeklyReportPlan(
        namespace="fixture",
        report_type="usage." + mode,
        owner_id=actor.actor_id,
        scope=AccessScope(type="node", id="node-a"),
        timezone="UTC",
        first_week=date(2026, 11, 16),
    )
    return scheduler, plan, history, actor, handler, clock


def query():
    return UsageHistoryQuery(
        node_id="node-a",
        start_at=datetime(2026, 11, 16, tzinfo=UTC),
        end_at=datetime(2026, 11, 23, tzinfo=UTC),
        timezone="UTC",
    )


@pytest.mark.parametrize("reader_type", [UsageHistoryService, IndexedUsageHistoryService])
def test_frozen_history_cannot_include_a_head_recorded_after_the_original_data_time(
    readings, reader_type
):
    _, (_, actor, _, _), receipt = readings
    _, _, indexed, _, _, _ = weekly(readings)
    service = reader_type(indexed.sessions, clock=indexed.clock)
    selection = UsageHistoryQuery(
        node_id="node-a",
        start_at=datetime(2026, 11, 16, 12, tzinfo=UTC),
        end_at=datetime(2026, 11, 16, 12, 2, tzinfo=UTC),
        snapshot=receipt.head,
    )
    with service.sessions() as session:
        with pytest.raises(DomainError) as error:
            service.read_frozen_in_transaction(session, actor, selection, read_at=selection.end_at)
    assert error.value.code == "VALIDATION_ERROR" and error.value.status == 422


def test_weekly_export_rejects_reconstructed_history_with_a_different_fingerprint(
    readings, monkeypatch
):
    scheduler, plan, history, actor, _, clock = weekly(readings)
    job = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0]).results[0].job.request
    assert scheduler.run_once(CONTEXT).state == "completed"
    original = history.read_frozen_in_transaction

    def altered(*args, **options):
        return original(*args, **options).model_copy(update={"fingerprint": "f" * 64})

    monkeypatch.setattr(history, "read_frozen_in_transaction", altered)
    with history.sessions() as session:
        with pytest.raises(DomainError) as error:
            WeeklyReportReader(history).materialize_in_transaction(
                session, actor, job.id, node_id="node-a"
            )
    assert error.value.code == "CONFLICT"


def test_weekly_report_survives_restart_and_keeps_the_original_ledger_prefix(readings):
    scheduler, plan, history, actor, _, clock = weekly(readings)
    batch = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    assert batch.caught_up and len(batch.results) == 1
    job = batch.results[0].job.request
    assert scheduler.run_once(CONTEXT).state == "completed"
    reader = WeeklyReportReader(history)
    report = reader.read(actor, job.id, node_id="node-a")
    assert report.history.recorded_microseconds == 30000000
    assert (
        report.history.total_usage_microseconds is None
        and report.history.source_coverage == "unknown"
    )
    assert len(report.history.days) == 7 and report.history.snapshot.revision == 1
    assert report.fingerprint == report.content_fingerprint()
    assert "rows" not in report.model_dump(mode="json")["history"]
    assert sum(gpu.recorded_microseconds for gpu in report.history.gpus) == 30000000
    with history.sessions() as session:
        _, original_details = reader.materialize_in_transaction(
            session, actor, job.id, node_id="node-a"
        )
    with history.sessions() as session:
        page = reader.list_in_transaction(session, actor, query())
        assert [value.id for value in page.items] == [job.id]
    readings[0][0].post(POST, post_window(readings[0], start=40, seq=101))
    assert history.read(actor, query()).snapshot.revision == 2
    clock[0] += timedelta(seconds=60)
    index_config = ledger_read_configuration("node-a")
    segments = LedgerSegmentService(
        history.sessions, configuration=index_config, clock=history.clock
    )
    segments.capture(
        UsageTraceConsumeContext(request_id="fixture-weekly-index"),
        CaptureLedgerSegment(
            id=uuid4(),
            target=history.read(actor, query()).snapshot,
            expected_configuration_fingerprint=index_config.fingerprint,
        ),
    )
    with history.sessions() as session:
        _, repeated_details = reader.materialize_in_transaction(
            session, actor, job.id, node_id="node-a"
        )
    assert repeated_details == original_details
    assert repeated_details.read_at == report.history.read_at
    assert WeeklyReportReader(history).read(actor, job.id, node_id="node-a") == report
    assert enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0]).results[0].duplicate
    assert scheduler.run_once(CONTEXT).state == "idle"


def test_report_generation_and_task_completion_share_one_transaction(readings):
    scheduler, plan, history, actor, handler, clock = weekly(readings)
    batch = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    id = batch.results[0].job.request.id

    class FailingAfterReport:
        validate = handler.validate

        def execute_in_transaction(self, session, request, deadline):
            handler.execute_in_transaction(session, request, deadline)
            session.flush()
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Synthetic failure after artifact", 503)

    failing = SchedulerService(
        history.sessions,
        tuple((definition, FailingAfterReport()) for definition in handler.definitions()),
        limits=SchedulerLimits(),
        clock=lambda: clock[0],
    )
    assert failing.run_once(CONTEXT).state == "retry"
    with history.sessions() as session:
        assert session.scalar(select(func.count()).select_from(WeeklyReportRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "usage.weekly_report.created")
            )
            == 0
        )
    clock[0] += timedelta(seconds=5)
    assert scheduler.run_once(CONTEXT).state == "completed"
    assert (
        WeeklyReportReader(history).read(actor, id, node_id="node-a").history.snapshot.revision == 1
    )
    with history.sessions() as session:
        assert session.scalar(select(func.count()).select_from(WeeklyReportRow)) == 1


def test_weekly_reads_require_current_view_permission_and_personal_owner(readings):
    scheduler, plan, history, actor, _, clock = weekly(readings, "node")
    batch = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    id = batch.results[0].job.request.id
    scheduler.run_once(CONTEXT)
    reader = WeeklyReportReader(history)
    assert reader.read(actor, id, node_id="node-a", mode="node").history.owner_id is None
    with pytest.raises(DomainError) as mismatch:
        reader.read(actor, id, node_id="node-a", mode="mine")
    assert mismatch.value.code == "PERMISSION_DENIED"
    with history.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
    with pytest.raises(DomainError) as revoked:
        reader.read(actor, id, node_id="node-a", mode="node")
    assert revoked.value.code == "PERMISSION_DENIED"


def test_weekly_artifact_is_immutable_and_read_only_queries_do_not_generate_it(readings):
    scheduler, plan, history, actor, _, clock = weekly(readings)
    reader = WeeklyReportReader(history)
    with history.sessions() as session:
        assert reader.list_in_transaction(session, actor, query()).items == ()
    batch = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    scheduler.run_once(CONTEXT)
    id = batch.results[0].job.request.id
    report = reader.read(actor, id, node_id="node-a")
    with pytest.raises(DBAPIError), history.sessions.begin() as session:
        session.execute(update(WeeklyReportRow).values(recorded_microseconds=0))
    if history.sessions.kw["bind"].dialect.name == "postgresql":
        with history.sessions() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            assert reader.list_in_transaction(session, actor, query()).items[0].id == id
            assert reader.read_in_transaction(session, actor, id, node_id="node-a") == report
            saved, details = reader.materialize_in_transaction(session, actor, id, node_id="node-a")
            assert saved == report and details.recorded_microseconds == 30000000


def test_schedule_change_is_a_conflict_and_unfinished_week_is_not_enqueued(readings):
    scheduler, plan, history, actor, _, clock = weekly(readings)
    before = enqueue_weekly_reports(
        scheduler, CONTEXT, plan, now=datetime(2026, 11, 22, tzinfo=UTC)
    )
    assert before.results == ()
    enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    with pytest.raises(DomainError) as changed:
        enqueue_weekly_reports(
            scheduler, CONTEXT, plan.model_copy(update={"timezone": "Asia/Tokyo"}), now=clock[0]
        )
    assert changed.value.code == "CONFLICT"


def test_existing_weekly_report_has_authenticated_list_detail_and_fixed_exports(readings, settings):
    scheduler, plan, history, actor, _, clock = weekly(readings)
    batch = enqueue_weekly_reports(scheduler, CONTEXT, plan, now=clock[0])
    scheduler.run_once(CONTEXT)
    id = str(batch.results[0].job.request.id)
    surface, _, _ = surface_for(readings, settings)
    surface.history = history
    surface.weekly_reports = WeeklyReportReader(history)
    surface.weekly_plans = WeeklyPlanReader(history.sessions, clock=lambda: clock[0])
    producer = WeeklyPlanService(
        history.sessions, scheduler, limits=WeeklyPreparationLimits(), clock=lambda: clock[0]
    )
    assert producer.run_once(CONTEXT, plan).state == "prepared"
    headers = {"Authorization": "Bearer " + TOKEN}
    selection = {"node_id": "node-a", "mode": "mine", "id": id}
    window = query()
    listing = {
        "node_id": "node-a",
        "mode": "mine",
        "window": {
            "kind": "utc",
            "start_at": window.start_at.isoformat(),
            "end_at": window.end_at.isoformat(),
            "timezone": "UTC",
        },
    }
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface)
    ) as client:
        assert client.post("/internal/usage/session", headers=headers).json()[
            "weekly_reports_available"
        ]
        plans = client.post(
            "/internal/usage/weekly/plans",
            headers=headers,
            json={"node_id": "node-a", "mode": "mine"},
        )
        assert (
            plans.status_code == 200
            and plans.json()["items"][0]["jobs"]["counts"]["completed"] == 1
        )
        assert plans.json()["items"][0]["worker_health"] == "not_assessed"
        assert (
            client.post(
                "/internal/usage/weekly/plans",
                headers=headers,
                json={"node_id": "node-a", "mode": "node"},
            ).json()["items"]
            == []
        )
        assert (
            client.post(
                "/internal/usage/weekly/plans",
                headers=headers,
                json={"node_id": "node-a", "owner_id": str(actor.actor_id)},
            ).status_code
            == 422
        )
        assert (
            client.post("/internal/usage/weekly/plans", json={"node_id": "node-a"}).status_code
            == 401
        )
        assert (
            client.post("/internal/usage/weekly/query", headers=headers, json=listing).json()[
                "items"
            ][0]["id"]
            == id
        )
        assert (
            client.post("/internal/usage/weekly/read", headers=headers, json=selection).json()[
                "history"
            ]["recorded_microseconds"]
            == 30000000
        )
        first = client.post("/internal/usage/weekly/report", headers=headers, json=selection)
        assert first.status_code == 200 and "<!doctype html>" in first.text.lower()
        assert "不代表全部实际用量" in first.text
        csv = client.post("/internal/usage/weekly/csv", headers=headers, json=selection)
        assert csv.status_code == 200 and "recorded" in csv.text
        readings[0][0].post(POST, post_window(readings[0], start=40, seq=101))
        assert (
            client.post("/internal/usage/weekly/report", headers=headers, json=selection).content
            == first.content
        )
        assert client.post("/internal/usage/weekly/read", json=selection).status_code == 401
        assert (
            client.post(
                "/internal/usage/weekly/create", headers=headers, json=selection
            ).status_code
            == 404
        )
        with history.sessions.begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
        assert (
            client.post(
                "/internal/usage/weekly/report", headers=headers, json=selection
            ).status_code
            == 403
        )


def test_private_weekly_operator_validates_without_db_and_runs_one_explicit_batch(
    readings, settings, tmp_path, capsys, monkeypatch
):
    import aris.apps.ops.weekly_reports as operator

    _, plan, history, _, _, _ = weekly(readings)
    config = tmp_path / "weekly.json"
    config.write_text(
        json.dumps(
            {
                "environment": "test",
                "database_url": settings.database_url,
                "plans": [
                    plan.model_copy(update={"first_week": date(2026, 11, 9)}).model_dump(
                        mode="json"
                    )
                ],
            }
        )
    )
    config.chmod(0o600)
    with monkeypatch.context() as patch:
        patch.setattr(
            operator, "build_engine", lambda *_: pytest.fail("validation accessed database")
        )
        assert weekly_main(["--config", str(config), "--validate"]) == 0
        assert not json.loads(capsys.readouterr().out)["database_accessed"]
    clock = readings[1][0].clock
    for service in (IndexedUsageHistoryService, SchedulerService, WeeklyPlanService):
        monkeypatch.setattr(operator, service.__name__, partial(service, clock=clock))
    assert weekly_main(["--config", str(config), "--once"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["plans"][0]["tasks_prepared"] == 1 and result["job_states"]["completed"] == 1
    assert not result["autostart_added"] and not result["notification_delivery"]
    assert weekly_main(["--config", str(config), "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["plans"][0]["state"] == "idle"
    with history.sessions() as session:
        assert session.scalar(select(func.count()).select_from(WeeklyReportRow)) == 1
