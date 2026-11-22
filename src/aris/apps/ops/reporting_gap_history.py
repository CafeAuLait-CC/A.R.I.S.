"""Inspect or capture a bounded set of observed reporting gaps."""

import argparse
import json
import signal
import sys
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from aris.apps.ops.identity import read_private
from aris.core.access.schemas import AccessContract
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory
from aris.modules.lyra.activity_preview import MAX_PREVIEW_MESSAGES
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.reporting_gap_history_schemas import MAX_SAVED_GAPS
from aris.modules.lyra.reporting_gap_history_service import ReportingGapHistoryService
from aris.modules.vega.usage_archive_schemas import MAX_ARCHIVE_SEQ, UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive


class GapHistoryDatabaseConfig(AccessContract):
    environment: Literal["development", "test"] = "development"
    database_url: SecretStr = Field(min_length=1, repr=False)
    actor_id: UUID


class GapHistoryConfig(GapHistoryDatabaseConfig):
    source: UsageArchiveHeadQuery


class ReportingScanBinding(AccessContract):
    source: UsageArchiveHeadQuery
    first_seq: int = Field(ge=1, le=MAX_ARCHIVE_SEQ, strict=True)


class GapHistoryWorkerConfig(GapHistoryDatabaseConfig):
    enabled: bool = Field(default=False, strict=True)
    sources: tuple[ReportingScanBinding, ...] = Field(min_length=1, max_length=8)
    poll_seconds: int = Field(default=60, ge=1, le=300, strict=True)

    @model_validator(mode="after")
    def distinct_nodes(self) -> "GapHistoryWorkerConfig":
        if len({binding.source.node_id for binding in self.sources}) != len(self.sources):
            raise ValueError("Choose one reporting source for each node")
        return self


def _run_scans(
    history: ReportingGapHistoryService,
    actor: ActorContext,
    config: GapHistoryWorkerConfig,
    maximum: int,
) -> None:
    stop_requested = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    for requested in (signal.SIGINT, signal.SIGTERM):
        signal.signal(requested, stop)
    next_scan = dict.fromkeys((binding.source.node_id for binding in config.sources), 0.0)
    previously: dict[str, tuple[str, int | None]] = {}
    while not stop_requested:
        for binding in config.sources:
            if stop_requested:
                break
            source = binding.source
            node = source.node_id
            if next_scan[node] > monotonic():
                continue
            state: tuple[str, int | None]
            try:
                status = history.scan_next(
                    actor, source, first_seq=binding.first_seq, max_messages=maximum
                )
            except DomainError as error:
                state = (error.code, None)
                next_scan[node] = monotonic() + config.poll_seconds
                if previously.get(node) != state:
                    print(
                        json.dumps({"node_id": node, "state": "failed", "error_code": error.code}),
                        flush=True,
                    )
            else:
                state = (status.state, status.missing_seq)
                next_scan[node] = (
                    monotonic() if status.state == "more" else monotonic() + config.poll_seconds
                )
                if status.saved or previously.get(node) != state:
                    print(status.model_dump_json(), flush=True)
            previously[node] = state
        resume = min(next_scan.values())
        while not stop_requested and monotonic() < resume:
            sleep(max(0.0, min(0.1, resume - monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-through", type=int, metavar="SEQ")
    mode.add_argument("--scan-next", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--list", action="store_true")
    parser.add_argument("--first-seq", type=int)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--before-seq", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.list and args.max_messages is not None:
        parser.error("--max-messages requires a scan or capture mode")
    if not args.list and (args.before_seq is not None or args.limit is not None):
        parser.error("--before-seq and --limit require --list")
    if args.scan_next != (args.first_seq is not None):
        parser.error("--first-seq is required for --scan-next")
    if args.first_seq is not None and not 1 <= args.first_seq <= MAX_ARCHIVE_SEQ:
        parser.error("--first-seq must be a valid sequence")
    if args.capture_through is not None and not 1 <= args.capture_through <= MAX_ARCHIVE_SEQ:
        parser.error("--capture-through requires a valid committed sequence")
    maximum = MAX_SAVED_GAPS if args.scan_next or args.run else MAX_PREVIEW_MESSAGES
    if args.max_messages is not None and not 1 <= args.max_messages <= maximum:
        parser.error("--max-messages must be within the archive preview range")
    if args.limit is not None and not 1 <= args.limit <= MAX_SAVED_GAPS:
        parser.error("--limit must be within the history read range")
    engine = None
    try:
        config = (
            read_private(args.config, GapHistoryWorkerConfig)
            if args.run
            else read_private(args.config, GapHistoryConfig)
        )
        if isinstance(config, GapHistoryWorkerConfig) and not config.enabled:
            print(json.dumps({"state": "paused", "database_accessed": False}))
            return 0
        url = make_url(config.database_url.get_secret_value())
        if url.get_backend_name() == "sqlite" and not Path(url.database or "").is_file():
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Existing migrated database is required", 503
            )
        engine = build_engine(config.database_url.get_secret_value())
        check_database(engine)
        sessions = session_factory(engine)
        history = ReportingGapHistoryService(
            ActivityPreviewService(sessions, VegaUsageArchive(sessions))
        )
        actor = ActorContext(actor_id=config.actor_id, request_id="reporting-gap-history")
        if isinstance(config, GapHistoryWorkerConfig):
            _run_scans(history, actor, config, args.max_messages or MAX_SAVED_GAPS)
        elif args.scan_next:
            print(
                history.scan_next(
                    actor,
                    config.source,
                    first_seq=args.first_seq,
                    max_messages=args.max_messages or MAX_SAVED_GAPS,
                ).model_dump_json()
            )
        elif args.capture_through is not None:
            print(
                history.capture(
                    actor,
                    config.source,
                    through_seq=args.capture_through,
                    max_messages=args.max_messages
                    if args.max_messages is not None
                    else MAX_SAVED_GAPS,
                ).model_dump_json()
            )
        else:
            records = history.latest(
                actor,
                config.source,
                before_seq=args.before_seq,
                limit=args.limit if args.limit is not None else MAX_SAVED_GAPS,
            )
            print(json.dumps({"records": [record.model_dump(mode="json") for record in records]}))
        return 0
    except (ValueError, OSError, DomainError, SQLAlchemyError) as error:
        code = (
            error.code
            if isinstance(error, DomainError)
            else (
                "DEPENDENCY_UNAVAILABLE"
                if isinstance(error, SQLAlchemyError)
                else "CONFIGURATION_ERROR"
            )
        )
        print(json.dumps({"state": "failed", "error_code": code}), file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
