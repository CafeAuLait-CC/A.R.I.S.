"""Manual opt-in calendar coverage coordinator; never installs a recurring service."""

import argparse
import json
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import Field, JsonValue, SecretStr, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.core.scheduler.schemas import SchedulerContext
from aris.modules.nova.coverage_schedule_schemas import CoverageScheduleDefinition
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.report_calendar import weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target


class WeeklyCoverageConfig(AccessContract):
    enabled: bool = Field(default=False, strict=True)
    environment: str = Field(default="development", pattern=r"^(development|test)$")
    database_url: SecretStr = Field(min_length=1, repr=False)
    plans: tuple[CoverageScheduleDefinition, ...] = Field(default=(), max_length=8)
    poll_seconds: int = Field(default=5, ge=1, le=60, strict=True)
    prefix_proofs_per_tick: int = Field(default=1, ge=1, le=8, strict=True)
    transaction_timeout_ms: int = Field(default=300000, ge=1000, le=300000, strict=True)

    @model_validator(mode="after")
    def explicit_target(self) -> "WeeklyCoverageConfig":
        try:
            url = make_url(self.database_url.get_secret_value())
        except ArgumentError:
            raise ValueError("weekly coverage database URL is invalid") from None
        if (
            url.drivername not in {"postgresql+psycopg", "sqlite+pysqlite"}
            or not url.database
            or url.database == ":memory:"
        ):
            raise ValueError("weekly coverage requires an existing persistent database")
        if (self.enabled and not self.plans) or len({plan.id for plan in self.plans}) != len(
            self.plans
        ):
            raise ValueError("weekly coverage requires explicit unique plans")
        return self


def run_tick(
    config: WeeklyCoverageConfig,
    schedules: CoverageScheduleService,
    *,
    status_only: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, JsonValue]:
    if not config.enabled and not status_only:
        return {
            "state": "paused",
            "current_quota_authorization": False,
            "plans": [],
            "made_progress": False,
        }
    now = (clock or (lambda: datetime.now(UTC)))()
    context = SchedulerContext(request_id="weekly-coverage-" + str(uuid4()))
    items: list[JsonValue] = []
    attention = False
    for definition in config.plans:
        try:
            progress = schedules.select(context, definition, status_only=status_only)
            if progress is None:
                items.append({"schedule_id": str(definition.id), "state": "not_registered"})
                continue
            period = weekly_report_period(definition.calendar, progress.next_week)
            if status_only or period.scheduled_for > now:
                items.append(
                    {
                        "schedule_id": str(definition.id),
                        "state": "pending" if period.scheduled_for <= now else "caught_up",
                        "progress": progress.model_dump(mode="json"),
                        "next_due_at": period.scheduled_for.isoformat(),
                    }
                )
                continue
            target = weekly_coverage_target(
                definition.calendar, period, definition.configuration, now=now
            )
            graph = build_lyra_coverage_components(
                schedules.sessions,
                LyraCoverageRuntimeConfig(
                    enabled=True,
                    configuration=definition.configuration,
                    target=CoverageBuildTarget(id=target.id, query=target.query),
                    prepare_prefix=True,
                    prefix_proofs_per_tick=config.prefix_proofs_per_tick,
                    transaction_timeout_ms=config.transaction_timeout_ms,
                ),
                clock=clock,
            )
            # Every finite phase owns its transaction. No coordinator lock spans it.
            step = graph.run_once()
            schedules.select(context, definition, status_only=True)
            if step["state"] == "finished":
                assert graph.service is not None
                saved = schedules.complete(context, definition, period.week, graph.service)
                items.append(
                    {
                        "schedule_id": str(definition.id),
                        "state": "snapshot_saved",
                        "result": saved.model_dump(mode="json"),
                    }
                )
            else:
                items.append(
                    {"schedule_id": str(definition.id), "state": step["state"], "build": step}
                )
        except DomainError as error:
            waiting = error.code in {"COVERAGE_SOURCE_NOT_CAUGHT_UP", "COVERAGE_BUILD_PENDING"}
            attention |= not waiting
            items.append(
                {
                    "schedule_id": str(definition.id),
                    "state": "waiting" if waiting else "failed",
                    "error_code": error.code,
                }
            )
    return {
        "state": "read_only" if status_only else "tick_processed",
        "plans": items,
        "needs_attention": attention,
        "made_progress": any(
            isinstance(item, dict)
            and item.get("state")
            in {"prefix_prepared", "admitted", "source_built", "snapshot_saved"}
            for item in items
        ),
        "current_quota_authorization": False,
        "notification_delivery": False,
        "autostart_added": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("validate", "status", "once", "run"):
        mode.add_argument("--" + flag, action="store_true")
    args = parser.parse_args(argv)
    engine = None
    stopped = Event()
    try:
        config = read_private(args.config, WeeklyCoverageConfig)
        if args.validate or (not config.enabled and not args.status):
            print(
                json.dumps(
                    {
                        "state": "valid" if args.validate else "paused",
                        "database_accessed": False,
                        "current_quota_authorization": False,
                    }
                )
            )
            return 0
        url = make_url(config.database_url.get_secret_value())
        if url.get_backend_name() == "sqlite" and not Path(url.database or "").is_file():
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Existing migrated database is required", 503
            )
        engine = build_engine(config.database_url.get_secret_value())
        check_database(engine)
        schedules = CoverageScheduleService(session_factory(engine), enabled=config.enabled)
        if args.run:
            for value in (signal.SIGINT, signal.SIGTERM):
                signal.signal(value, lambda *_: stopped.set())
        while True:
            result = run_tick(config, schedules, status_only=args.status)
            print(json.dumps(result), flush=True)
            if args.once or args.status:
                return 1 if result.get("needs_attention") else 0
            delay = (
                config.poll_seconds
                if result.get("needs_attention") or not result.get("made_progress")
                else 0
            )
            if stopped.wait(delay):
                return 1 if result.get("needs_attention") else 0
    except (ValueError, OSError, DomainError, SQLAlchemyError) as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error_code": error.code
                    if isinstance(error, DomainError)
                    else "DEPENDENCY_UNAVAILABLE"
                    if isinstance(error, SQLAlchemyError)
                    else "CONFIGURATION_ERROR",
                }
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
