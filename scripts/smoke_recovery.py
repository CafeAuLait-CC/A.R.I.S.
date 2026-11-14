"""Exercise real Collector CLI offline persistence and replay in disposable state."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from smoke import smoke_database
from sqlalchemy import create_engine, text


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def start_api(env: dict[str, str], port: int, log, client: httpx.Client) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aris.apps.api.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=log,
        stderr=log,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("disposable API exited")
            try:
                if client.get("/health/ready").status_code == 200:
                    return process
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        raise RuntimeError("disposable API readiness timeout")
    except BaseException:
        stop_api(process)
        raise


def stop_api(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def pending(path: Path) -> tuple[dict[str, str], dict[int, str]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return (
            dict(db.execute("SELECT key,value FROM metadata")),
            dict(db.execute("SELECT seq,payload FROM pending ORDER BY seq")),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--real-gpu", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    (root / ".local").mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="recovery-smoke-", dir=root / ".local") as temporary,
        smoke_database(Path(temporary), args.postgres) as database_url,
    ):
        state = Path(temporary)
        spool = state / "spool.sqlite3"
        read_token, node_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = {
            **os.environ,
            "ARIS_ENVIRONMENT": "test",
            "ARIS_DATABASE_URL": database_url,
            "ARIS_READ_TOKEN": read_token,
            "ARIS_AGENT_CREDENTIALS": json.dumps(
                {
                    "recovery-agent": {
                        "node_id": "recovery-node",
                        "token": node_token,
                    }
                }
            ),
            "ARIS_AGENT_ID": "recovery-agent",
            "ARIS_AGENT_NODE_ID": "recovery-node",
            "ARIS_AGENT_TOKEN": node_token,
            "ARIS_AGENT_SPOOL_PATH": str(spool),
            "ARIS_AGENT_RESOURCE_ENABLED": "true",
            "ARIS_AGENT_LIVE_ENABLED": "false",
        }
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env["ARIS_AGENT_API_URL"] = f"http://127.0.0.1:{port}"
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True, timeout=30
        )
        agent = [sys.executable, "-m", "aris.apps.agent.main", "--once"]
        if not args.real_gpu:
            agent += ["--fixture", "examples/gpu-idle.json"]

        def run_agent(expected: int) -> None:
            result = subprocess.run(agent, env=env, timeout=30, capture_output=True)
            if result.returncode != expected:
                raise RuntimeError("unexpected Collector CLI exit status")

        engine = create_engine(database_url)
        process = None
        with (
            (state / "api.log").open("ab") as log,
            httpx.Client(base_url=env["ARIS_AGENT_API_URL"], timeout=2, trust_env=False) as client,
        ):
            try:
                process = start_api(env, port, log, client)
                run_agent(0)
                inode = spool.stat().st_ino
                assert pending(spool)[1] == {}
                stop_api(process)
                run_agent(1)
                metadata, buffered = pending(spool)
                assert metadata["last_seq"] == "4"
                assert sorted(buffered) == [3, 4]
                exact = {seq: json.loads(raw) for seq, raw in buffered.items()}
                assert exact[3]["schema_version"] == "aris.telemetry/v2"
                assert exact[4]["schema_version"] == "aris.heartbeat/v1"
                with engine.connect() as db:
                    assert db.scalar(text("SELECT count(*) FROM agent_messages")) == 2
                process = start_api(env, port, log, client)
                # New health facts can be persisted alongside the old, unacknowledged
                # heartbeat. Latest facts go first; both original messages survive.
                run_agent(0)
                assert pending(spool)[1] == {}
                with engine.connect() as db:
                    rows = db.execute(
                        text(
                            "SELECT seq,payload,fingerprint FROM agent_messages WHERE seq IN (3,4)"
                        )
                    ).all()
                    assert len(rows) == 2
                    for row in rows:
                        payload = (
                            json.loads(row.payload) if isinstance(row.payload, str) else row.payload
                        )
                        assert payload == exact[row.seq]
                        assert row.fingerprint == fingerprint(exact[row.seq])
                headers = {"Authorization": "Bearer " + read_token}
                current = client.get("/public/v2/resources", headers=headers)
                current.raise_for_status()
                assert current.json()["nodes"][0]["resource_seq"] == 5
                run_agent(0)
                metadata_after, remaining = pending(spool)
                assert remaining == {} and metadata_after["last_seq"] == "8"
                assert spool.stat().st_ino == inode
                assert metadata_after["agent_id"] == metadata["agent_id"] == "recovery-agent"
                assert metadata_after["node_id"] == metadata["node_id"] == "recovery-node"
                with engine.connect() as db:
                    for table in ("agent_messages", "domain_events", "event_outbox", "audit_logs"):
                        assert db.scalar(text("SELECT count(*) FROM " + table)) == 8
                    assert list(
                        db.scalars(text("SELECT seq FROM agent_messages ORDER BY seq"))
                    ) == list(range(1, 9))
                recovered = client.get("/public/v2/resources", headers=headers)
                recovered.raise_for_status()
                node = recovered.json()["nodes"][0]
                assert node["node"]["status"] == "ONLINE"
                assert node["node"]["telemetry_status"] == "CURRENT"
                assert node["resource_seq"] == 7
                assert node["system_freshness"] == "CURRENT"
                print(
                    json.dumps(
                        {
                            "result": "PASS",
                            "backend": "PostgreSQL" if args.postgres else "SQLite",
                            "mode": "real-gpu" if args.real_gpu else "fixture",
                            "durable_messages": 8,
                            "observed_offline_messages": 2,
                            "exact_replayed_payloads": 2,
                            "original_spool_inode_and_identity_preserved": True,
                            "current_projection_not_overwritten_by_old_history": True,
                            "pending": 0,
                        }
                    )
                )
            finally:
                if process is not None:
                    stop_api(process)
                engine.dispose()


if __name__ == "__main__":
    main()
