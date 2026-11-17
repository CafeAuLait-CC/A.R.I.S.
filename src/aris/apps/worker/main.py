"""Inspect events, process Home jobs, or run an explicitly configured M2 worker."""

import argparse
import json
import time
from pathlib import Path

from sqlalchemy import func, select

from aris.apps.composition.m2 import (
    M2RuntimeConfig,
    WorkerMode,
    build_m2_components,
    load_m2_config,
)
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


def run_m2_worker(
    settings: Settings,
    slack: SlackSettings,
    config: M2RuntimeConfig,
    mode: WorkerMode,
    once: bool = False,
) -> None:
    engine = build_engine(settings.database_url)
    try:
        check_database(engine)
        components = build_m2_components(session_factory(engine), settings, slack, config)
        while True:
            try:
                print(json.dumps(components.run_once(mode)), flush=True)
            except Exception as error:
                print(
                    json.dumps({"mode": "m2-" + mode, "error_type": type(error).__name__}),
                    flush=True,
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
        description="Inspect events, process Home jobs with --slack, or select an explicit M2 mode"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--m2-config", type=Path)
    parser.add_argument("--m2-mode", choices=["commands", "events", "notifications", "home", "all"])
    parser.add_argument(
        "--slack", action="store_true", help="Run durable Slack Home publishing jobs"
    )
    args = parser.parse_args()
    if (args.m2_config is None) != (args.m2_mode is None):
        parser.error("--m2-config and --m2-mode must be provided together")
    if args.m2_config is not None:
        if args.slack:
            parser.error("--slack and --m2-mode select different workers")
        run_m2_worker(
            Settings(), SlackSettings(), load_m2_config(args.m2_config), args.m2_mode, args.once
        )
        return
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
