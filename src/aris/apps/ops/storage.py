import argparse
import json
from datetime import UTC, datetime, timedelta

from aris.core.config import Settings
from aris.core.db import build_engine, check_database, session_factory
from aris.core.storage import inspect_pending_events, inspect_storage
from aris.modules.vega.service import VegaService


def run() -> None:
    parser = argparse.ArgumentParser(description="Read-only capacity and retention preview")
    parser.add_argument("--older-than-days", type=int, help="Count old messages; never delete them")
    parser.add_argument(
        "--pending-types", action="store_true", help="Include bounded pending event-type counts"
    )
    args = parser.parse_args()
    if args.older_than_days is not None and args.older_than_days < 1:
        parser.error("--older-than-days must be positive")
    settings = Settings()
    engine = build_engine(settings.database_url)
    try:
        check_database(engine)
        cutoff = (
            datetime.now(UTC) - timedelta(days=args.older_than_days)
            if args.older_than_days is not None
            else None
        )
        observations = VegaService(session_factory(engine), settings).storage(cutoff)
        report = {
            "read_only": True,
            "storage": inspect_storage(engine).model_dump(mode="json"),
            "observations": observations.model_dump(mode="json"),
        }
        if args.pending_types:
            report["pending_events"] = inspect_pending_events(engine).model_dump(mode="json")
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
