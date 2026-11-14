import argparse
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from aris.apps.agent.collector import collect
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.live import LiveClient
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.sampler import Sampler
from aris.apps.agent.schemas import AgentInspection
from aris.apps.agent.spool import Spool
from aris.apps.agent.transport import DeliveryWorker, flush, report
from aris.modules.vega.resource_schemas import ResourceInspection
from aris.modules.vega.schemas import GpuSample


class CollectionFailed(RuntimeError):
    """GPU collection failed; persist its health independently from delivery."""


def collect_tick(
    spool: Spool,
    fixture: Path | None = None,
    sampler: Sampler | ResourceSampler | None = None,
) -> dict[str, int]:
    error: Exception | None = None
    collected = False
    try:
        if isinstance(sampler, ResourceSampler):
            resources = sampler.sample()
            collected = resources.gpu.quality == "ok"
            spool.enqueue_resource(socket.gethostname(), resources)
            if not collected:
                error = CollectionFailed("GPU collection unavailable")
        else:
            observation = sampler.sample() if sampler else None
            samples = (
                observation.gpus
                if observation
                else (
                    TypeAdapter(list[GpuSample]).validate_json(fixture.read_text())
                    if fixture
                    else collect()
                )
            )
            collected = True
            spool.enqueue(
                socket.gethostname(), samples, observation.observed_at if observation else None
            )
    except Exception as exc:
        error = exc
    heartbeat = spool.enqueue_heartbeat(
        socket.gethostname(),
        "ok" if collected else "error",
        resource_enabled=isinstance(sampler, ResourceSampler),
    )
    if error:
        if not collected:
            raise CollectionFailed(type(error).__name__) from None
        raise error
    return {"seq": heartbeat.seq}


def tick(
    spool: Spool,
    settings: AgentSettings,
    client: httpx.Client,
    fixture: Path | None = None,
    sampler: Sampler | ResourceSampler | None = None,
) -> dict[str, int]:
    try:
        result = collect_tick(spool, fixture, sampler)
    finally:
        # Capacity and collection failures must still allow pending data to drain.
        delivered = flush(spool, settings, client)
    return {**result, "delivered": delivered}


def main() -> None:
    parser = argparse.ArgumentParser(description="Vega Collector: read-only NVIDIA GPU telemetry")
    parser.add_argument("--once", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--fixture", type=Path, help="Use explicit fixture GPU samples; no GPU needed"
    )
    source.add_argument(
        "--inspect-resources",
        action="store_true",
        help="Print read-only GPU, CPU/RAM and compute counts without credentials or network",
    )
    source.add_argument(
        "--inspect",
        action="store_true",
        help="Print one real local GPU snapshot without credentials, network or spool",
    )
    args = parser.parse_args()
    if args.inspect_resources:
        inspection_resources = ResourceSampler().sample()
        print(
            ResourceInspection(
                hostname=socket.gethostname(), resources=inspection_resources
            ).model_dump_json()
        )
        if inspection_resources.gpu.quality != "ok":
            raise SystemExit(1)
        return
    if args.inspect:
        try:
            samples = collect()
            inspection = AgentInspection(
                hostname=socket.gethostname(), observed_at=datetime.now(UTC), gpus=samples
            )
            print(inspection.model_dump_json())
        except Exception as exc:
            print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
            raise SystemExit(1) from None
        return
    os.umask(0o077)
    settings = AgentSettings()
    spool = Spool(
        settings.spool_path,
        settings.id,
        settings.node_id,
        settings.max_buffered_batches,
        settings.max_buffered_heartbeats,
    )
    sampler_type = ResourceSampler if settings.resource_enabled else Sampler
    sampler = sampler_type(
        (lambda: TypeAdapter(list[GpuSample]).validate_json(args.fixture.read_text()))
        if args.fixture
        else collect
    )
    live = LiveClient(settings, sampler) if settings.live_enabled and not args.once else None
    with ExitStack() as resources:
        if live:
            live.start()
            resources.callback(live.close)
        if args.once:
            try:
                with httpx.Client(timeout=15, trust_env=False) as client:
                    result = tick(spool, settings, client, args.fixture, sampler)
                print(json.dumps({**result, "buffer": spool.statistics()}), flush=True)
            except Exception as exc:
                print(
                    json.dumps({"error_type": type(exc).__name__, "buffer_retained": True}),
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(1) from None
            return
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        sender = DeliveryWorker(spool, settings)
        sender.start()
        resources.callback(sender.close)
        while not stopped.is_set():
            if not sender.thread.is_alive():
                raise RuntimeError("delivery worker stopped")
            started = time.monotonic()
            try:
                result = collect_tick(spool, args.fixture, sampler)
                report({"phase": "sample", **result, "buffer": spool.statistics()})
            except Exception as exc:
                report(
                    {
                        "phase": "sample",
                        "error_type": type(exc).__name__,
                        "buffer_retained": True,
                        "buffer": spool.statistics(),
                    }
                )
            finally:
                sender.wake()
            delay = max(0, settings.interval_seconds - (time.monotonic() - started))
            stopped.wait(delay + random.uniform(0, 1))


if __name__ == "__main__":
    main()
