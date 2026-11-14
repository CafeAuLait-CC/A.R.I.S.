"""Run a loopback API + Agent + Worker with disposable state and optional real GPUs."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gpu", action="store_true", help="Collect real NVIDIA GPUs")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    (root / ".local").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=root / ".local") as temporary:
        state = Path(temporary)
        read_token, agent_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = {
            **os.environ,
            "ARIS_ENVIRONMENT": "test",
            "ARIS_DATABASE_URL": f"sqlite+pysqlite:///{state / 'aris.db'}",
            "ARIS_READ_TOKEN": read_token,
            "ARIS_AGENT_CREDENTIALS": json.dumps(
                {"smoke-agent": {"node_id": "smoke-node", "token": agent_token}}
            ),
            "ARIS_AGENT_ID": "smoke-agent",
            "ARIS_AGENT_NODE_ID": "smoke-node",
            "ARIS_AGENT_TOKEN": agent_token,
            "ARIS_AGENT_SPOOL_PATH": str(state / "spool.sqlite3"),
        }
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        env["ARIS_AGENT_API_URL"] = f"http://127.0.0.1:{port}"
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True, timeout=30
        )
        with (state / "api.log").open("w+") as log:
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
                with httpx.Client(
                    base_url=env["ARIS_AGENT_API_URL"], trust_env=False, timeout=2
                ) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            raise RuntimeError("API exited before readiness")
                        try:
                            if client.get("/health/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("API readiness timeout")
                    agent_command = [sys.executable, "-m", "aris.apps.agent.main", "--once"]
                    if not args.real_gpu:
                        agent_command += ["--fixture", "examples/gpu-idle.json"]
                    subprocess.run(
                        agent_command,
                        env=env,
                        check=True,
                        timeout=30,
                    )
                    response = client.get(
                        "/api/public/v1/resources",
                        headers={"Authorization": f"Bearer {read_token}"},
                    )
                    response.raise_for_status()
                    node = response.json()["nodes"][0]
                    assert node["node_id"] == "smoke-node"
                    assert node["status"] == "ONLINE"
                    assert node["gpus"]
                    if args.real_gpu:
                        assert all(gpu["present"] for gpu in node["gpus"])
                        assert all(
                            gpu["observed_state"] in {"idle", "busy"} for gpu in node["gpus"]
                        )
                    else:
                        assert node["gpus"][0]["observed_state"] == "idle"
                    print(
                        json.dumps(
                            {
                                "mode": "real_gpu" if args.real_gpu else "fixture",
                                "observed_at": node["observed_at"],
                                "gpus": node["gpus"],
                            }
                        )
                    )
                subprocess.run(
                    [sys.executable, "-m", "aris.apps.worker.main", "--once"],
                    env=env,
                    check=True,
                    timeout=30,
                )
                mode = "real GPU Agent" if args.real_gpu else "simulated Agent"
                print(f"PASS: migrated database -> loopback API -> {mode} -> status -> Worker")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
