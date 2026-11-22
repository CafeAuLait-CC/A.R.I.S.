"""Manual opt-in calendar coverage coordinator; never installs a recurring service."""

import argparse
import json
import signal
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, SecretStr, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from aris.apps.composition.daily_coverage import (
    daily_coverage_step,
    prepare_day_revision,
    verify_daily_coverage,
)
from aris.apps.composition.lyra_coverage import (
    CoverageBuildTarget,
    LyraCoverageRuntimeConfig,
    build_lyra_coverage_components,
)
from aris.apps.composition.weekly_retention import preview_week_retention
from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.core.scheduler.schemas import SchedulerContext
from aris.interfaces.usage.combined_week_export import combined_week_csv, combined_week_html
from aris.modules.nova.coverage_schedule_schemas import CoverageScheduleDefinition
from aris.modules.nova.coverage_schedule_service import CoverageScheduleService
from aris.modules.nova.daily_coverage_calendar import daily_coverage_targets
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
    from_daily_week: date | None = None,
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
            if from_daily_week is not None:
                items.append(
                    daily_coverage_step(
                        schedules.sessions,
                        definition,
                        from_daily_week,
                        clock=clock or (lambda: datetime.now(UTC)),
                        status_only=status_only,
                        prefix_proofs_per_tick=config.prefix_proofs_per_tick,
                        transaction_timeout_ms=config.transaction_timeout_ms,
                    )
                )
                continue
            progress = schedules.select(context, definition, status_only=status_only)
            if progress is None:
                items.append({"schedule_id": str(definition.id), "state": "not_registered"})
                continue
            period = weekly_report_period(definition.calendar, progress.next_week)
            if definition.daily_rollups:
                step = daily_coverage_step(
                    schedules.sessions,
                    definition,
                    period.week,
                    clock=clock or schedules.clock,
                    status_only=status_only,
                    prefix_proofs_per_tick=config.prefix_proofs_per_tick,
                    transaction_timeout_ms=config.transaction_timeout_ms,
                    include_weekly=False,
                )
                if step["state"] == "report_ready" and not status_only:
                    saved_daily = schedules.complete_daily(context, definition, period.week)
                    step.update(
                        state="daily_report_saved", result=saved_daily.model_dump(mode="json")
                    )
                items.append(step)
                continue
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
            in {
                "prefix_prepared",
                "admitted",
                "source_built",
                "snapshot_saved",
                "day_saved",
                "daily_report_saved",
            }
            for item in items
        ),
        "current_quota_authorization": False,
        "notification_delivery": False,
        "autostart_added": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--from-daily-week",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Prepare each local day of one explicit Monday week and preview its report",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("validate", "status", "once", "run"):
        mode.add_argument("--" + flag, action="store_true")
    mode.add_argument(
        "--verify-day",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Recheck a published report day against its retained original evidence",
    )
    mode.add_argument(
        "--review-week",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Show the saved evidence checks of one Monday week",
    )
    mode.add_argument(
        "--retention-week",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Inspect retention dependencies for one Monday week without changing records",
    )
    mode.add_argument(
        "--combine-week",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Combine saved node reports for one Monday week",
    )
    mode.add_argument(
        "--day-timeline",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Read consolidated intervals for a day referenced by a saved weekly report",
    )
    mode.add_argument(
        "--prepare-day-revision",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Build or review an unpublished replacement for one saved reporting day",
    )
    parser.add_argument("--revision-node", help="Select the node whose reporting day is revised")
    parser.add_argument("--revision-id", type=UUID, help="Use the same ID when resuming a revision")
    parser.add_argument(
        "--format",
        choices=("json", "html", "csv"),
        help="Output format for --combine-week (default: json)",
    )
    parser.add_argument(
        "--expected-combined-fingerprint",
        help="Require an exact previously viewed combined report",
    )
    parser.add_argument(
        "--record-verification",
        action="store_true",
        help="Save the successful check of --verify-day",
    )
    args = parser.parse_args(argv)
    review = any(
        value is not None
        for value in (
            args.verify_day,
            args.review_week,
            args.retention_week,
            args.combine_week,
            args.day_timeline,
            args.prepare_day_revision,
        )
    )
    if review and args.from_daily_week is not None:
        parser.error("Choose one operation on a saved natural week")
    if args.record_verification and args.verify_day is None:
        parser.error("--record-verification requires --verify-day")
    if args.prepare_day_revision is not None and (
        args.revision_id is None or args.revision_node is None
    ):
        parser.error("--prepare-day-revision requires --revision-node and --revision-id")
    if args.prepare_day_revision is None and (
        args.revision_id is not None or args.revision_node is not None
    ):
        parser.error("--revision-id and --revision-node require --prepare-day-revision")
    if args.combine_week is None and (
        args.format is not None or args.expected_combined_fingerprint is not None
    ):
        parser.error("--format and --expected-combined-fingerprint require --combine-week")
    engine = None
    stop_requested = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stop_requested
        # Signal callbacks may reenter: do not acquire a threading primitive here.
        stop_requested = True

    try:
        config = read_private(args.config, WeeklyCoverageConfig)
        if args.from_daily_week is not None:
            for plan in config.plans:
                daily_coverage_targets(plan, args.from_daily_week)
        if review and (not config.plans or any(not plan.daily_rollups for plan in config.plans)):
            raise ValueError("Verification requires configured daily coverage plans")
        if args.record_verification and not config.enabled:
            raise DomainError(
                "WEEKLY_COVERAGE_NOT_CONFIGURED", "Saving coverage evidence is paused", 503
            )
        if args.validate or (not config.enabled and not args.status and not review):
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
        if args.prepare_day_revision is not None:
            selected = tuple(
                plan for plan in config.plans if plan.calendar.scope.id == args.revision_node
            )
            if len(selected) != 1:
                raise ValueError("Day revision requires one configured plan for the selected node")
            revision = prepare_day_revision(
                schedules,
                selected[0],
                args.prepare_day_revision,
                args.revision_id,
                prefix_proofs_per_tick=config.prefix_proofs_per_tick,
                transaction_timeout_ms=config.transaction_timeout_ms,
            )
            print(json.dumps(revision))
            return 0
        if args.retention_week is not None:
            print(
                json.dumps(
                    {
                        "state": "retention_preview",
                        "plans": [
                            preview_week_retention(schedules, plan, args.retention_week).model_dump(
                                mode="json"
                            )
                            for plan in config.plans
                        ],
                        "deletion_enabled": False,
                    }
                )
            )
            return 0
        if args.day_timeline is not None:
            timelines = [
                schedules.saved_day_timeline(plan, args.day_timeline).model_dump(mode="json")
                for plan in config.plans
            ]
            print(json.dumps({"state": "saved_day_timelines", "plans": timelines}))
            return 0
        if args.combine_week is not None:
            report = schedules.combined_week(config.plans, args.combine_week)
            if (
                args.expected_combined_fingerprint is not None
                and args.expected_combined_fingerprint != report.fingerprint
            ):
                raise DomainError("CONFLICT", "Combined weekly report has changed", 409)
            if args.format == "html":
                print(combined_week_html(report))
            elif args.format == "csv":
                print(combined_week_csv(report), end="")
            else:
                print(json.dumps(report.model_dump(mode="json")))
            return 0
        if args.verify_day is not None:
            print(
                json.dumps(
                    {
                        "state": "verified",
                        "plans": [
                            verify_daily_coverage(
                                schedules,
                                plan,
                                args.verify_day,
                                transaction_timeout_ms=config.transaction_timeout_ms,
                                record=args.record_verification,
                            )
                            for plan in config.plans
                        ],
                        "deletion_enabled": False,
                    }
                )
            )
            return 0
        if args.review_week is not None:
            print(
                json.dumps(
                    {
                        "state": "evidence_review",
                        "plans": [
                            {
                                "schedule_id": str(plan.id),
                                **schedules.evidence_review(plan, args.review_week).model_dump(
                                    mode="json"
                                ),
                            }
                            for plan in config.plans
                        ],
                        "deletion_enabled": False,
                    }
                )
            )
            return 0
        if args.run:
            for value in (signal.SIGINT, signal.SIGTERM):
                signal.signal(value, request_stop)
        while True:
            result = run_tick(
                config, schedules, status_only=args.status, from_daily_week=args.from_daily_week
            )
            print(json.dumps(result), flush=True)
            if args.once or args.status:
                return 1 if result.get("needs_attention") else 0
            if args.from_daily_week is not None:
                plans = result["plans"]
                if (
                    isinstance(plans, list)
                    and plans
                    and all(
                        isinstance(plan, dict) and plan.get("state") == "report_ready"
                        for plan in plans
                    )
                ):
                    return 0
            delay = (
                config.poll_seconds
                if result.get("needs_attention") or not result.get("made_progress")
                else 0
            )
            resume_at = monotonic() + delay
            while not stop_requested:
                remaining = resume_at - monotonic()
                if remaining <= 0:
                    break
                # Keep the configured database poll interval; this wait does no I/O.
                sleep(min(0.1, remaining))
            if stop_requested:
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
