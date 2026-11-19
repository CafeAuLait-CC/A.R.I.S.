"""Explicit opt-in runner; never migrate, initialize identities or start other services."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from aris.apps.composition.lyra import LyraComponents, LyraRuntimeConfig, build_lyra_components
from aris.core.config import Settings
from aris.core.db import build_engine, check_database, session_factory


def _existing_target(settings: Settings) -> None:
    url = make_url(settings.database_url)
    if url.drivername == "postgresql+psycopg":
        return
    if url.drivername in {"sqlite", "sqlite+pysqlite"} and settings.environment == "test":
        if url.database and url.database != ":memory:" and Path(url.database).is_file():
            return
    raise ValueError("Lyra recording requires PostgreSQL or an existing test-only SQLite database")


def run_lyra_worker(
    config: LyraRuntimeConfig, once: bool = False, *, settings: Settings | None = None
) -> None:
    config = LyraRuntimeConfig.model_validate(config.model_dump(mode="json"))
    if not config.enabled:
        graph = LyraComponents(None, config.fingerprint, lambda: datetime.now(UTC))
        print(json.dumps(graph.run_once()), flush=True)
        return
    engine: Engine | None = None
    try:
        settings = settings or Settings()
        _existing_target(settings)
        engine = build_engine(settings.database_url)
        check_database(engine)
        components = build_lyra_components(session_factory(engine), config)
        failures = 0
        remaining = len(config.sources)
        cycle_unsuccessful = False
        while True:
            try:
                report = components.run_once()
                unsuccessful = bool(report["failures"])
            except Exception as error:
                report = {
                    "mode": "lyra-recording",
                    "paused": False,
                    "configuration_fingerprint": config.fingerprint,
                    "quota_usage_available": False,
                    "time_window_coverage": "not_assessed",
                    "error_type": type(error).__name__,
                }
                unsuccessful = True
            print(json.dumps(report), flush=True)
            if once:
                cycle_unsuccessful = cycle_unsuccessful or unsuccessful
                remaining -= 1
                if remaining == 0:
                    if cycle_unsuccessful:
                        raise SystemExit(1) from None
                    return
                continue
            failures = min(6, failures + 1) if unsuccessful else 0
            time.sleep(min(60, config.poll_seconds * 2**failures))
    except Exception as error:
        print(
            json.dumps(
                {
                    "mode": "lyra-recording",
                    "paused": False,
                    "configuration_fingerprint": config.fingerprint,
                    "quota_usage_available": False,
                    "time_window_coverage": "not_assessed",
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()
