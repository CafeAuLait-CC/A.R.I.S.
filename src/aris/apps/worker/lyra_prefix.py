"""Explicit finite source-prefix maintenance; never migrate or delete source history."""

import json
import time
from datetime import UTC, datetime

from sqlalchemy import Engine

from aris.apps.composition.lyra_prefix import (
    LyraPrefixComponents,
    LyraPrefixRuntimeConfig,
    build_lyra_prefix_components,
)
from aris.apps.worker.lyra import _existing_target
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.db import build_engine, check_database, session_factory


def run_lyra_prefix_worker(
    config: LyraPrefixRuntimeConfig,
    once: bool = False,
    *,
    status_only: bool = False,
    settings: Settings | None = None,
) -> None:
    config = LyraPrefixRuntimeConfig.model_validate(config.model_dump(mode="json"))
    if (
        (not config.enabled and not status_only)
        or config.configuration is None
        or config.before is None
    ):
        graph = LyraPrefixComponents(None, config, lambda: datetime.now(UTC))
        print(json.dumps(graph.run_once(status_only=status_only)), flush=True)
        return
    engine: Engine | None = None
    try:
        settings = settings or Settings()
        _existing_target(settings)
        engine = build_engine(settings.database_url)
        check_database(engine)
        graph = build_lyra_prefix_components(
            session_factory(engine), config, status_only=status_only
        )
        failures = 0
        while True:
            try:
                report = graph.run_once(status_only=status_only)
                unsuccessful = False
            except Exception as error:
                report = {
                    "mode": "lyra-source-prefix",
                    "state": "failed",
                    "configuration_fingerprint": config.fingerprint,
                    "source_deletion_enabled": False,
                    "quota_usage_available": False,
                    "error_type": type(error).__name__,
                }
                if isinstance(error, DomainError):
                    report["code"] = error.code
                unsuccessful = True
            print(json.dumps(report), flush=True)
            if once or status_only or report["state"] in {"boundary_reached", "caught_up"}:
                if unsuccessful:
                    raise SystemExit(1) from None
                return
            failures = min(6, failures + 1) if unsuccessful else 0
            time.sleep(min(60, config.poll_seconds * 2**failures))
    except Exception as error:
        print(
            json.dumps(
                {
                    "mode": "lyra-source-prefix",
                    "state": "failed",
                    "configuration_fingerprint": config.fingerprint,
                    "source_deletion_enabled": False,
                    "quota_usage_available": False,
                    "error_type": type(error).__name__,
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
    finally:
        if engine is not None:
            engine.dispose()
