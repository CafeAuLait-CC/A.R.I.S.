"""Explicit one-batch weekly report operator; no automatic startup or delivery."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, SecretStr, ValidationError, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.core.scheduler.schemas import SchedulerContext, SchedulerLimits
from aris.core.scheduler.service import SchedulerService
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.weekly_report_service import WeeklyReportHandler
from aris.modules.nova.report_calendar import WeeklyReportPlan
from aris.modules.nova.report_scheduler import enqueue_weekly_reports


class WeeklyReportConfig(AccessContract):
    environment: Literal["development", "test"]
    database_url: SecretStr = Field(min_length=1, repr=False)
    plans: tuple[WeeklyReportPlan, ...] = Field(min_length=1, max_length=8)
    limits: SchedulerLimits = Field(default_factory=SchedulerLimits)

    @model_validator(mode="after")
    def explicit_reports(self) -> "WeeklyReportConfig":
        url = make_url(self.database_url.get_secret_value())
        if (
            url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}
            or not url.database
            or url.database == ":memory:"
        ):
            raise ValueError("weekly reports require an explicit persistent database")
        keys = [
            (plan.namespace, plan.report_type, plan.scope, plan.owner_id) for plan in self.plans
        ]
        if len(set(keys)) != len(keys) or any(
            plan.scope.type != "node" or plan.report_type not in ("usage.mine", "usage.node")
            for plan in self.plans
        ):
            raise ValueError("weekly plans require unique explicit node report views")
        return self


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and execute one bounded weekly-report batch"
    )
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--once", action="store_true")
    parser.add_argument("--from-week", type=date.fromisoformat)
    parser.add_argument("--max-jobs", type=int, default=8)
    args = parser.parse_args(argv)
    if not 1 <= args.max_jobs <= 32:
        parser.error("use a job batch between 1 and 32")
    engine = None
    try:
        config = read_private(args.config, WeeklyReportConfig)
        if args.from_week is not None and (
            len(config.plans) != 1
            or args.from_week.weekday() != 0
            or args.from_week < config.plans[0].first_week
        ):
            raise ValueError("continuation requires one plan and an enabled Monday")
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
        scheduler = SchedulerService(
            history.sessions,
            tuple((definition, handler) for definition in handler.definitions()),
            limits=config.limits,
        )
        context = SchedulerContext(request_id="weekly-" + str(uuid4()))
        now = datetime.now(UTC)
        prepared = []
        for plan in config.plans:
            batch = enqueue_weekly_reports(
                scheduler, context, plan, now=now, from_week=args.from_week, limit=8
            )
            prepared.append(
                {
                    "namespace": plan.namespace,
                    "report_type": plan.report_type,
                    "enqueued": sum(not item.duplicate for item in batch.results),
                    "duplicates": sum(item.duplicate for item in batch.results),
                    "next_week": batch.next_week.isoformat(),
                    "caught_up": batch.caught_up,
                }
            )
        states: dict[str, int] = {}
        for _ in range(args.max_jobs):
            tick = scheduler.run_once(context)
            states[tick.state] = states.get(tick.state, 0) + 1
            if tick.state == "idle":
                break
        print(
            json.dumps(
                {
                    "result": "batch_processed",
                    "plans": prepared,
                    "job_states": states,
                    "autostart_added": False,
                    "notification_delivery": False,
                    "needs_attention": any(
                        state in states for state in ("retry", "blocked", "dead", "lost_claim")
                    ),
                }
            )
        )
        return (
            3 if any(state in states for state in ("retry", "blocked", "dead", "lost_claim")) else 0
        )
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
        if engine is not None:
            engine.dispose()
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
