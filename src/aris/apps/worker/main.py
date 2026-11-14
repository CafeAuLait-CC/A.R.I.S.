"""Foundation worker: inspect dependencies and outbox; dispatch is an M2 task."""

import argparse
import json
import time

from sqlalchemy import func, select

from aris.core.config import Settings
from aris.core.db import build_engine, check_database, session_factory
from aris.core.events import OutboxEntry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect pending events; no delivery in this version"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    engine = build_engine(Settings().database_url)
    sessions = session_factory(engine)
    try:
        while True:
            check_database(engine)
            with sessions() as session:
                pending = session.scalar(
                    select(func.count())
                    .select_from(OutboxEntry)
                    .where(OutboxEntry.published_at.is_(None))
                )
            print(
                json.dumps(
                    {"mode": "inspect_only", "pending_events": pending, "dispatch_enabled": False}
                ),
                flush=True,
            )
            if args.once:
                return
            time.sleep(30)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
