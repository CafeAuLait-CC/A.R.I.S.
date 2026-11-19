"""Explicit bounded tick or manual weekly Worker; never installs an automatic service."""

import argparse
import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import Field, SecretStr, ValidationError, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.core.scheduler.schemas import SchedulerContext, SchedulerLimits, TaskQueueQuery
from aris.core.scheduler.service import SchedulerService
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_service import WEEKLY_KINDS, WeeklyReportHandler
from aris.modules.nova.report_calendar import WeeklyReportPlan
from aris.modules.nova.weekly_plan_schemas import WeeklyPreparationLimits, plan_id, plan_key
from aris.modules.nova.weekly_plan_service import WeeklyPlanService


class WeeklyReportConfig(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    plans: tuple[WeeklyReportPlan, ...] = Field(min_length=1, max_length=8)
    limits: SchedulerLimits = Field(default_factory=SchedulerLimits)
    preparation: WeeklyPreparationLimits = Field(default_factory=WeeklyPreparationLimits)
    poll_seconds: int = Field(default=30, ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def explicit_reports(self) -> "WeeklyReportConfig":
        url = make_url(self.database_url.get_secret_value())
        if (
            url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}
            or not url.database
            or url.database == ":memory:"
        ):
            raise ValueError("weekly reports require an explicit persistent database")
        if len({plan_id(plan) for plan in self.plans}) != len(self.plans) or any(
            plan.scope.type != "node" or plan.report_type not in ("usage.mine", "usage.node")
            for plan in self.plans
        ):
            raise ValueError("weekly plans require unique explicit node report views")
        if (
            self.preparation.lease_seconds
            <= self.preparation.max_seconds + self.limits.transaction_timeout_ms / 1000
        ):
            raise ValueError("weekly lease must outlive preparation and a task transaction")
        return self


def run_tick(
    config: WeeklyReportConfig, producer: WeeklyPlanService, worker: SchedulerService, max_jobs: int
) -> dict[str, object]:
    context = SchedulerContext(request_id="weekly-" + str(uuid4()))
    prepared = []
    attention = False
    for plan in config.plans:
        try:
            tick = producer.run_once(context, plan)
            attention |= tick.state in ("retry", "lost_claim")
            prepared.append(
                {
                    "plan_id": str(plan_id(plan)),
                    "state": tick.state,
                    "tasks_prepared": len(tick.task_ids),
                    "next_week": tick.next_week.isoformat() if tick.next_week else None,
                    "caught_up": tick.caught_up,
                    "error_code": tick.error_code,
                }
            )
        except DomainError as error:
            attention = True
            prepared.append(
                {"plan_id": str(plan_id(plan)), "state": "error", "error_code": error.code}
            )
    states: dict[str, int] = {}
    for _ in range(max_jobs):
        try:
            result = worker.run_once(context)
        except DomainError as error:
            attention = True
            states[error.code] = states.get(error.code, 0) + 1
            break
        states[result.state] = states.get(result.state, 0) + 1
        attention |= result.state in ("retry", "blocked", "dead", "lost_claim")
        if result.state == "idle":
            break
    return {
        "result": "tick_processed",
        "plans": prepared,
        "job_states": states,
        "needs_attention": attention,
        "autostart_added": False,
        "notification_delivery": False,
    }


def read_status(
    config: WeeklyReportConfig, producer: WeeklyPlanService, worker: SchedulerService
) -> dict[str, object]:
    values = []
    attention = False
    for plan in config.plans:
        try:
            value = producer.status(plan)
            jobs = worker.queue(
                TaskQueueQuery(
                    kind=WEEKLY_KINDS["mine" if plan.report_type == "usage.mine" else "node"],
                    owner_id=plan.owner_id,
                    scope=plan.scope,
                    key_prefix=plan_key(plan) + "/",
                )
            )
            producer.status(plan)
            attention |= bool(
                value.last_error_code
                or jobs.overdue_count
                or jobs.counts["dead"]
                or jobs.counts["blocked"]
            )
            values.append(
                {
                    "plan_id": str(plan_id(plan)),
                    "state": "saved_plan",
                    "next_week": value.next_week.isoformat(),
                    "next_due_at": value.next_due_at.isoformat(),
                    "last_prepared_at": value.last_prepared_at.isoformat()
                    if value.last_prepared_at
                    else None,
                    "last_error_code": value.last_error_code,
                    "retry_at": value.retry_at.isoformat() if value.retry_at else None,
                    "cursor_meaning": "tasks_prepared",
                    "worker_health": "not_assessed",
                    "jobs": jobs.model_dump(mode="json"),
                }
            )
        except DomainError as error:
            attention |= error.code != "NOT_FOUND"
            values.append(
                {
                    "plan_id": str(plan_id(plan)),
                    "state": "not_registered" if error.code == "NOT_FOUND" else "unavailable",
                    "error_code": error.code,
                }
            )
    return {
        "result": "read_only",
        "plans": values,
        "needs_attention": attention,
        "notification_delivery": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run persistent weekly-report preparation and registered tasks"
    )
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("validate", "once", "run", "status"):
        mode.add_argument("--" + flag, action="store_true")
    parser.add_argument("--max-jobs", type=int, default=8)
    parser.add_argument("--max-seconds", type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.max_jobs <= 32 or (
        args.max_seconds is not None and (not args.run or not 1 <= args.max_seconds <= 86400)
    ):
        parser.error("use 1..32 jobs and, only for --run, a positive runtime up to one day")
    engine = None
    stopped = Event()
    previous_signal = None
    try:
        config = read_private(args.config, WeeklyReportConfig)
        if args.validate:
            print(
                json.dumps(
                    {
                        "result": "valid",
                        "plans": len(config.plans),
                        "database_accessed": False,
                        "autostart_added": False,
                        "notification_delivery": False,
                    }
                )
            )
            return 0
        url = make_url(config.database_url.get_secret_value())
        if url.drivername == "sqlite+pysqlite" and not Path(str(url.database)).is_file():
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Existing migrated database is required", 503
            )
        engine = build_engine(config.database_url.get_secret_value())
        check_database(engine)
        history = UsageHistoryService(session_factory(engine))
        handler = WeeklyReportHandler(history)
        worker = SchedulerService(
            history.sessions,
            tuple((definition, handler) for definition in handler.definitions()),
            limits=config.limits,
        )
        producer = WeeklyPlanService(history.sessions, worker, limits=config.preparation)
        if args.status:
            status = read_status(config, producer, worker)
            print(json.dumps(status))
            return 3 if status["needs_attention"] else 0
        if args.once:
            report = run_tick(config, producer, worker, args.max_jobs)
            print(json.dumps(report))
            return 3 if report["needs_attention"] else 0
        previous_signal = signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        start = last_log = monotonic()
        end = start + args.max_seconds if args.max_seconds is not None else float("inf")
        print(
            json.dumps(
                {
                    "result": "worker_started",
                    "started_at": datetime.now(UTC).isoformat(),
                    "autostart_added": False,
                    "notification_delivery": False,
                }
            ),
            flush=True,
        )
        while not stopped.is_set() and monotonic() < end:
            report = run_tick(config, producer, worker, args.max_jobs)
            plans = report["plans"]
            states = report["job_states"]
            active = (
                report["needs_attention"]
                or (isinstance(states, dict) and any(state != "idle" for state in states))
                or (isinstance(plans, list) and any(plan["state"] == "prepared" for plan in plans))
            )
            if active or monotonic() - last_log >= 900:
                print(json.dumps(report), flush=True)
                last_log = monotonic()
            stopped.wait(max(0, min(config.poll_seconds, end - monotonic())))
        print(json.dumps({"result": "worker_stopped"}), flush=True)
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"result": "worker_stopped"}), flush=True)
        return 0
    except (ValidationError, ValueError, OSError):
        error = {
            "code": "CONFIGURATION_ERROR",
            "message": "Private weekly report configuration is invalid",
        }
    except DomainError as rejected:
        error = {"code": rejected.code, "message": rejected.message}
    except SQLAlchemyError:
        error = {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": "Weekly report database is unavailable",
        }
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
        if engine is not None:
            engine.dispose()
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
