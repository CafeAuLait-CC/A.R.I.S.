"""Inspect or capture a bounded set of observed reporting gaps."""

import argparse
import json
import sys
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
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


class GapHistoryConfig(AccessContract):
    environment: Literal["development", "test"] = "development"
    database_url: SecretStr = Field(min_length=1, repr=False)
    actor_id: UUID
    source: UsageArchiveHeadQuery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-through", type=int, metavar="SEQ")
    mode.add_argument("--scan-next", action="store_true")
    mode.add_argument("--list", action="store_true")
    parser.add_argument("--first-seq", type=int)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--before-seq", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.list and args.max_messages is not None:
        parser.error("--max-messages requires --capture-through or --scan-next")
    if not args.list and (args.before_seq is not None or args.limit is not None):
        parser.error("--before-seq and --limit require --list")
    if args.scan_next != (args.first_seq is not None):
        parser.error("--first-seq is required for --scan-next")
    if args.first_seq is not None and not 1 <= args.first_seq <= MAX_ARCHIVE_SEQ:
        parser.error("--first-seq must be a valid sequence")
    if args.capture_through is not None and not 1 <= args.capture_through <= MAX_ARCHIVE_SEQ:
        parser.error("--capture-through requires a valid committed sequence")
    maximum = MAX_SAVED_GAPS if args.scan_next else MAX_PREVIEW_MESSAGES
    if args.max_messages is not None and not 1 <= args.max_messages <= maximum:
        parser.error("--max-messages must be within the archive preview range")
    if args.limit is not None and not 1 <= args.limit <= MAX_SAVED_GAPS:
        parser.error("--limit must be within the history read range")
    engine = None
    try:
        config = read_private(args.config, GapHistoryConfig)
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
        if args.scan_next:
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
