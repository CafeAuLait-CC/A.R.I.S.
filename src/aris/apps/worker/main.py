"""Inspect the domain Outbox, or explicitly process Slack Home jobs."""

import argparse
import json
import time

from sqlalchemy import func, select

from aris.core.config import Settings
from aris.core.db import build_engine, check_database, session_factory
from aris.core.events import OutboxEntry
from aris.interfaces.slack.client import SlackClient
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.dispatch import SlackDispatcher
from aris.modules.vega.service import VegaService


def run_slack_worker(settings: Settings, slack: SlackSettings, once: bool = False) -> None:
    if not slack.configured:
        raise RuntimeError("Slack worker requires configured credentials and workspace")
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    dispatcher = SlackDispatcher(
        sessions, slack, VegaService(sessions, settings), SlackClient(slack)
    )
    try:
        check_database(engine)
        while True:
            try:
                result = dispatcher.run_once()
                print(json.dumps({"mode": "slack_jobs", **result.model_dump()}), flush=True)
            except Exception as exc:
                print(
                    json.dumps({"mode": "slack_jobs", "error_type": type(exc).__name__}), flush=True
                )
                if once:
                    raise SystemExit(1) from None
            if once:
                return
            time.sleep(5)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect domain events, or process Slack Home jobs with --slack"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--slack", action="store_true", help="Run durable Slack Home publishing jobs"
    )
    args = parser.parse_args()
    if args.slack:
        run_slack_worker(Settings(), SlackSettings(), args.once)
        return
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
