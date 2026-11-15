"""Accelerated historical backlog recovery in disposable state, never the application DB."""

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from smoke import smoke_database
from sqlalchemy import create_engine, text

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.spool import Spool
from aris.apps.agent.transport import flush
from aris.apps.api.main import create_app
from aris.core.config import Settings
from aris.modules.vega.resource_schemas import ResourceObservation


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def observation(at: datetime) -> ResourceObservation:
    return ResourceObservation.model_validate(
        {
            "observed_at": at,
            "gpu": {
                "observed_at": at,
                "quality": "ok",
                "gpus": [
                    {
                        "gpu_uuid": "backlog-fixture-GPU",
                        "index": 0,
                        "model": "explicit fixture",
                        "memory_total_mib": 4096,
                        "memory_used_mib": 0,
                        "utilization_percent": 0,
                    }
                ],
            },
            "system": {
                "observed_at": at,
                "cpu": {"quality": "unsupported"},
                "memory": {"quality": "unsupported"},
            },
            "compute": {"observed_at": at, "quality": "unknown"},
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--cycles", type=int, default=512)
    parser.add_argument("--interval-seconds", type=int, default=15)
    args = parser.parse_args()
    if not 2 <= args.cycles <= 20000 or not 1 <= args.interval_seconds <= 3600:
        parser.error("cycles must be 2–20000; interval must be 1–3600 seconds")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    (root / ".local").mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="backlog-smoke-", dir=root / ".local") as temporary,
        smoke_database(Path(temporary), args.postgres) as database_url,
    ):
        token, read_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        config = Settings(
            _env_file=None,
            environment="test",
            database_url=database_url,
            read_token=read_token,
            agent_credentials={"backlog-agent": {"node_id": "backlog-node", "token": token}},
        )
        agent = AgentSettings(
            _env_file=None,
            id="backlog-agent",
            node_id="backlog-node",
            token=token,
            max_buffered_batches=args.cycles + 1,
            max_buffered_heartbeats=args.cycles + 1,
        )
        path = Path(temporary) / "original.sqlite3"
        spool = Spool(path, agent.id, agent.node_id, args.cycles + 1, args.cycles + 1)
        at = datetime.now(UTC) - timedelta(seconds=args.cycles * args.interval_seconds)

        class HistoricalClock:
            @staticmethod
            def now(tz):
                return at.astimezone(tz)

        # Construct original messages under a controlled clock. No stored message
        # is edited to create history, and no real outage duration is claimed.
        with patch("aris.apps.agent.spool.datetime", HistoricalClock):
            for _ in range(args.cycles):
                spool.enqueue_resource("fixture-node", observation(at))
                spool.enqueue_heartbeat("fixture-node", "ok")
                at += timedelta(seconds=args.interval_seconds)
        spool.enqueue_resource("fixture-node", observation(datetime.now(UTC)))
        newest = spool.enqueue_heartbeat("fixture-node", "ok")
        with sqlite3.connect(path) as db:
            expected = {
                seq: fingerprint(json.loads(payload))
                for seq, payload in db.execute("SELECT seq,payload FROM pending")
            }
        inode = path.stat().st_ino
        maximum_pending = len(expected)
        duplicate_receipts = 0
        lose_ack = True
        current_resource_seq = newest.seq - 1

        # create_app intentionally does not run migrations. Only this disposable
        # database/schema is migrated, using a private subprocess environment.
        import subprocess
        import sys

        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "ARIS_DATABASE_URL": database_url, "ARIS_ENVIRONMENT": "test"},
            check=True,
            timeout=30,
        )
        for phase in range(2):
            spool = Spool(path, agent.id, agent.node_id, args.cycles + 1, args.cycles + 1)
            with TestClient(create_app(config)) as api:

                def send(request):
                    nonlocal duplicate_receipts, lose_ack
                    response = api.post(
                        request.url.path,
                        content=request.content,
                        headers={"X-Agent-Token": token, "Content-Type": "application/json"},
                    )
                    assert response.status_code == 200
                    payload = response.json()
                    duplicate_receipts += int(payload["duplicate"])
                    if lose_ack:
                        lose_ack = False
                        raise httpx.ReadError("simulated response loss after commit")
                    return httpx.Response(200, json=payload)

                with httpx.Client(transport=httpx.MockTransport(send)) as client:
                    while (
                        spool.statistics()["pending_batches"]
                        + spool.statistics()["pending_heartbeats"]
                    ):
                        try:
                            flush(spool, agent, client)
                        except httpx.ReadError:
                            assert phase == 0
                            assert len(spool.pending()) == maximum_pending
                            continue
                        current = api.get(
                            "/public/resources", headers={"Authorization": "Bearer " + read_token}
                        ).json()["nodes"][0]
                        assert current["resource_seq"] == current_resource_seq
                        # For long runs the last sample may become stale; it must
                        # retain its original time rather than be refreshed by replay.
                        assert current["node"]["seq"] == newest.seq
                        remaining = spool.statistics()
                        count = remaining["pending_batches"] + remaining["pending_heartbeats"]
                        if phase == 0 and count <= maximum_pending // 2:
                            break
            assert path.stat().st_ino == inode
        assert spool.pending() == []
        assert spool.statistics()["last_seq"] == newest.seq
        assert duplicate_receipts == 1
        engine = create_engine(database_url)
        try:
            with engine.connect() as db:
                for table in ("agent_messages", "domain_events", "event_outbox", "audit_logs"):
                    assert db.scalar(text("SELECT count(*) FROM " + table)) == maximum_pending
                for seq, payload, stored in db.execute(
                    text("SELECT seq,payload,fingerprint FROM agent_messages")
                ):
                    payload = json.loads(payload) if isinstance(payload, str) else payload
                    assert expected[seq] == stored == fingerprint(payload)
        finally:
            engine.dispose()
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "backend": "PostgreSQL" if args.postgres else "SQLite",
                    "mode": "accelerated fixture; not a real timed outage",
                    "historical_cycles": args.cycles,
                    "represented_hours": args.cycles * args.interval_seconds / 3600,
                    "exact_messages": maximum_pending,
                    "lost_ack_duplicate_receipts": duplicate_receipts,
                    "API_and_spool_reopened_mid_recovery": True,
                    "current_facts_precede_history_without_regression": True,
                    "original_spool_inode_preserved": True,
                    "pending": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
