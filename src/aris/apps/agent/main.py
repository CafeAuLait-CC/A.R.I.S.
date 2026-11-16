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
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from aris.apps.agent.collector import collect
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.disk_collector import DiskCollector
from aris.apps.agent.live import LiveClient
from aris.apps.agent.lock import CollectorAlreadyRunning, collector_lock
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.spool import Spool
from aris.apps.agent.spool_status import inspect_spool
from aris.apps.agent.transport import DeliveryWorker, flush, report
from aris.modules.vega.resource_schemas import ResourceInspection
from aris.modules.vega.schemas import GpuSample


class CollectionFailed(RuntimeError):
    """GPU collection failed; persist its health independently from delivery."""


def collect_tick(
    spool: Spool,
    fixture: Path | None = None,
    sampler: ResourceSampler | None = None,
) -> dict[str, int]:
    error: Exception | None = None
    collected = False
    try:
        if sampler is None:
            sampler = ResourceSampler(
                (lambda: TypeAdapter(list[GpuSample]).validate_json(fixture.read_text()))
                if fixture
                else collect
            )
        resources = sampler.sample()
        collected = resources.gpu.quality == "ok"
        spool.enqueue_resource(socket.gethostname(), resources)
        if not collected:
            error = CollectionFailed("GPU collection unavailable")
    except Exception as exc:
        error = exc
    heartbeat = spool.enqueue_heartbeat(
        socket.gethostname(),
        "ok" if collected else "error",
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
    sampler: ResourceSampler | None = None,
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
        "--inspect",
        action="store_true",
        help="Print read-only GPU, CPU/RAM and compute counts without credentials or network",
    )
    source.add_argument(
        "--spool-status",
        action="store_true",
        help="Read existing local backlog without sampling, delivery, initialization or repair",
    )
    args = parser.parse_args()
    if args.spool_status:
        if args.once:
            parser.error("--spool-status cannot be combined with --once")
        try:
            settings = AgentSettings()
            status = inspect_spool(
                settings.spool_path,
                settings.id,
                settings.node_id,
                max_batches=settings.max_buffered_batches,
                max_heartbeats=settings.max_buffered_heartbeats,
                interval_seconds=settings.interval_seconds,
            )
        except Exception as exc:
            print(json.dumps({"status": "unknown", "error_type": type(exc).__name__}))
            raise SystemExit(1) from None
        print(json.dumps(status))
        if status["status"] != "ok":
            raise SystemExit(1)
        return
    if args.inspect:
        observation = ResourceSampler().sample()
        print(
            ResourceInspection(
                hostname=socket.gethostname(), resources=observation
            ).model_dump_json()
        )
        if observation.gpu.quality != "ok":
            raise SystemExit(1)
        return
    os.umask(0o077)
    settings = AgentSettings()
    try:
        with collector_lock(settings.spool_path):
            run_collector(settings, args.fixture, args.once)
    except CollectorAlreadyRunning:
        print(json.dumps({"error_type": "CollectorAlreadyRunning"}), file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def run_collector(settings: AgentSettings, fixture: Path | None, once: bool) -> None:
    spool = Spool(
        settings.spool_path,
        settings.id,
        settings.node_id,
        settings.max_buffered_batches,
        settings.max_buffered_heartbeats,
    )
    resolved_spool = settings.spool_path.resolve()
    disks = (
        None
        if fixture
        else DiskCollector(
            resolved_spool.with_name(resolved_spool.name + ".disks.json"),
            enabled=not once,
            emit=report,
        )
    )
    if disks is not None and disks.cache_error:
        report({"phase": "disk_cache", "cache_error": True, "disk_queries_enabled": False})
    sampler = ResourceSampler(
        (lambda: TypeAdapter(list[GpuSample]).validate_json(fixture.read_text()))
        if fixture
        else collect,
        disks=disks,
    )
    live = LiveClient(settings, sampler) if settings.live_enabled and not once else None
    with ExitStack() as resources:
        if disks is not None:
            resources.callback(disks.close)
        if live:
            live.start()
            resources.callback(live.close)
        if once:
            try:
                with httpx.Client(timeout=15, trust_env=False) as client:
                    result = tick(spool, settings, client, fixture, sampler)
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
                result = collect_tick(spool, fixture, sampler)
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
