"""Exercise real outbound WebSockets/SSE on ephemeral loopback ports, never the app DB."""

import argparse
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from aris.apps.agent.collector import collect, parse_nvidia_csv
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.live import LiveClient
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.api.main import create_app
from aris.core.config import Settings
from aris.interfaces.slack.config import SlackSettings
from aris.modules.vega.live_schemas import LiveSnapshot


def smoke(real_gpu: bool) -> dict[str, object]:
    root = Path(".local")
    root.mkdir(exist_ok=True)
    calls: list[float] = []

    def sample():
        calls.append(time.monotonic())
        return (
            collect()
            if real_gpu
            else parse_nvidia_csv("GPU-example-only,0,Simulated NVIDIA GPU,49140,0,0,32\n")
        )

    with tempfile.TemporaryDirectory(prefix="live-smoke-", dir=root) as temporary:
        database = Path(temporary) / "unused.sqlite3"
        config = Settings(
            _env_file=None,
            environment="test",
            database_url=f"sqlite+pysqlite:///{database}",
            read_token="live-smoke-read-credential",
            agent_credentials={
                "live-smoke": {"node_id": "live-smoke", "token": "live-smoke-node-credential"}
            },
        )
        app = create_app(config, SlackSettings(_env_file=None))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                access_log=False,
                timeout_graceful_shutdown=2,
                ws_max_size=65536,
            )
        )
        server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        collector = LiveClient(
            AgentSettings(
                _env_file=None,
                id="live-smoke",
                node_id="live-smoke",
                api_url=base,
                token="live-smoke-node-credential",
                live_enabled=True,
            ),
            ResourceSampler(sample),
        )
        server_thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if time.monotonic() > deadline:
                    raise RuntimeError("live smoke server did not start")
                time.sleep(0.01)
            collector.start()
            time.sleep(0.2)
            assert calls == [], "No viewer must mean no high-frequency driver reads"
            frames = []
            with httpx.Client(timeout=10, trust_env=False) as client:
                with client.stream(
                    "GET",
                    base + "/internal/live",
                    headers={"Authorization": "Bearer " + config.read_token.get_secret_value()},
                ) as response:
                    response.raise_for_status()
                    deadline = time.monotonic() + 15
                    for line in response.iter_lines():
                        if time.monotonic() > deadline:
                            raise RuntimeError("live samples did not arrive")
                        if not line.startswith("data: "):
                            continue
                        snapshot = LiveSnapshot.model_validate_json(line[6:])
                        for node in snapshot.nodes:
                            if node.quality == "live" and (
                                not frames or node.seq != frames[-1].seq
                            ):
                                frames.append(node)
                        if len(frames) >= 3:
                            break
            assert len(frames) >= 3
            assert len({frame.observed_at for frame in frames}) == len(frames)
            resource_result = {}
            last = frames[-1]
            assert last.system is not None and last.compute is not None
            assert len({frame.system_observed_at for frame in frames}) == len(frames)
            assert len({frame.compute_observed_at for frame in frames}) == 1
            if real_gpu:
                assert last.system.cpu.quality == "ok"
                assert last.system.memory.quality == "ok"
                assert last.compute.quality == "ok"
            resource_result = {
                "system_freshness": last.system_freshness,
                "cpu_quality": last.system.cpu.quality,
                "cpu_logical_count": last.system.cpu.logical_count,
                "memory_quality": last.system.memory.quality,
                "compute_quality": last.compute.quality,
                "compute_own_observation_time_preserved": True,
                "process_user_scope": last.compute.user_scope,
            }
            time.sleep(1.5)
            calls_after_stop = len(calls)
            time.sleep(1.2)
            assert len(calls) == calls_after_stop, "Driver reads continued after last viewer left"
            assert not database.exists(), "Preview path opened a database"
            return {
                "result": "PASS",
                "mode": "real-gpu" if real_gpu else "fixture",
                "unique_preview_frames": len(frames),
                "gpus": len(frames[-1].gpus),
                "sample_intervals_seconds": [
                    round((right.observed_at - left.observed_at).total_seconds(), 3)
                    for left, right in zip(frames, frames[1:], strict=False)
                ],
                "driver_reads_stopped_without_viewers": True,
                "database_created": database.exists(),
                "spool_used": False,
                **resource_result,
            }
        finally:
            if collector.thread.is_alive():
                collector.close()
            server.should_exit = True
            server_thread.join(timeout=5)
            listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gpu", action="store_true")
    args = parser.parse_args()
    print(json.dumps(smoke(args.real_gpu)))
