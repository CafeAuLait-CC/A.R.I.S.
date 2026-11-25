import argparse
import json
from datetime import UTC, datetime, timedelta

from aris.core.config import Settings
from aris.core.db import build_engine, check_database, session_factory
from aris.core.storage import inspect_pending_events, inspect_storage
from aris.modules.lyra.retention_origin_service import preview_pre_recording_retention
from aris.modules.lyra.retention_service import inspect_retention_dependencies
from aris.modules.vega.service import VegaService
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


def run() -> None:
    parser = argparse.ArgumentParser(description="Read-only capacity and retention preview")
    parser.add_argument("--older-than-days", type=int, help="Count old messages; never delete them")
    parser.add_argument(
        "--pending-types", action="store_true", help="Include bounded pending event-type counts"
    )
    parser.add_argument(
        "--retention-dependencies", action="store_true", help="Include Lyra source dependencies"
    )
    parser.add_argument(
        "--pre-recording-source",
        nargs=2,
        metavar=("AGENT", "NODE"),
        help="Inspect an early archive excluded by known recording origins; never delete",
    )
    parser.add_argument("--retention-before", type=datetime.fromisoformat)
    parser.add_argument("--retention-timezone", help="IANA timezone of the natural-week cutoff")
    args = parser.parse_args()
    if args.older_than_days is not None and args.older_than_days < 1:
        parser.error("--older-than-days must be positive")
    origin_args = (args.pre_recording_source, args.retention_before, args.retention_timezone)
    if any(value is not None for value in origin_args) and not all(
        value is not None for value in origin_args
    ):
        parser.error("pre-recording inspection requires source, before and timezone")
    if args.pre_recording_source and (
        args.older_than_days or args.pending_types or args.retention_dependencies
    ):
        parser.error("pre-recording inspection cannot be combined with other storage views")
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        check_database(engine)
        if args.pre_recording_source:
            agent, node = args.pre_recording_source
            result = preview_pre_recording_retention(
                session_factory(engine),
                UsageArchiveHeadQuery(agent_id=agent, node_id=node),
                before=args.retention_before,
                timezone=args.retention_timezone,
            )
            print(result.model_dump_json())
            return
        cutoff = (
            datetime.now(UTC) - timedelta(days=args.older_than_days)
            if args.older_than_days is not None
            else None
        )
        sessions = session_factory(engine)
        observations = VegaService(sessions, settings).storage(cutoff)
        report = {
            "read_only": True,
            "storage": inspect_storage(engine).model_dump(mode="json"),
            "observations": observations.model_dump(mode="json"),
        }
        if args.pending_types:
            report["pending_events"] = inspect_pending_events(engine).model_dump(mode="json")
        if args.retention_dependencies:
            report["retention_dependencies"] = inspect_retention_dependencies(sessions).model_dump(
                mode="json"
            )
        print(json.dumps(report))
    finally:
        engine.dispose()


def main() -> None:
    try:
        run()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "read_only": True}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
