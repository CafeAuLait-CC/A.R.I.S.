import argparse
import json
import os
import random
import socket
import sys
import time
from collections.abc import Callable
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
from aris.modules.vega.resource_schemas import ResourceInspection, ResourceTelemetryBatch
from aris.modules.vega.schemas import GpuSample, Heartbeat, IngestResult


class CollectionFailed(RuntimeError):
    """Heartbeat delivered, but GPU collection failed; keep the heartbeat cadence."""


def flush(
    spool: Spool,
    settings: AgentSettings,
    client: httpx.Client,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    delivered = 0
    started = clock()
    batches = spool.delivery_batch(settings.flush_max_messages)
    for batch in batches:
        endpoint = "heartbeat" if isinstance(batch, Heartbeat) else "telemetry/batch"
        version = "v2" if isinstance(batch, ResourceTelemetryBatch) else "v1"
        response = client.post(
            f"{settings.api_url.rstrip('/')}/vega/{version}/{endpoint}",
            headers={"X-Agent-Token": settings.token.get_secret_value()},
            json=batch.model_dump(mode="json"),
        )
        response.raise_for_status()
        receipt = IngestResult.model_validate(response.json())
        if receipt.message_id != batch.message_id:
            raise ValueError("mismatched agent acknowledgement")
        spool.acknowledge(batch)
        delivered += 1
        # Soft budget: reserve heartbeat + current snapshot + one historical message.
        if delivered >= 3 and clock() - started >= settings.flush_budget_seconds:
            break
    return delivered


def tick(
    spool: Spool,
    settings: AgentSettings,
    client: httpx.Client,
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
    try:
        heartbeat = spool.enqueue_heartbeat(
            socket.gethostname(),
            "ok" if collected else "error",
            resource_enabled=isinstance(sampler, ResourceSampler),
        )
    finally:
        # Even a full spool or a broken collector must allow the backlog to drain.
        delivered = flush(spool, settings, client)
    if error:
        if not collected:
            raise CollectionFailed(type(error).__name__) from None
        raise error
    return {"seq": heartbeat.seq, "delivered": delivered}


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
    spool = Spool(settings.spool_path, settings.id, settings.node_id, settings.max_buffered_batches)
    sampler_type = ResourceSampler if settings.resource_enabled else Sampler
    sampler = sampler_type(
        (lambda: TypeAdapter(list[GpuSample]).validate_json(args.fixture.read_text()))
        if args.fixture
        else collect
    )
    live = LiveClient(settings, sampler) if settings.live_enabled and not args.once else None
    failures = 0
    with ExitStack() as resources:
        if live:
            live.start()
            resources.callback(live.close)
        client = resources.enter_context(httpx.Client(timeout=15, trust_env=False))
        while True:
            try:
                result = tick(spool, settings, client, args.fixture, sampler)
                print(json.dumps({**result, "buffer": spool.statistics()}), flush=True)
                failures = 0
            except Exception as exc:
                # No response body, credentials, URLs or process command output in logs.
                print(
                    json.dumps({"error_type": type(exc).__name__, "buffer_retained": True}),
                    file=sys.stderr,
                    flush=True,
                )
                if args.once:
                    raise SystemExit(1) from None
                failures = 0 if isinstance(exc, CollectionFailed) else failures + 1
            if args.once:
                return
            delay = min(settings.interval_seconds * (2 ** min(failures, 4)), 60)
            time.sleep(delay + random.uniform(0, 1))


if __name__ == "__main__":
    main()
