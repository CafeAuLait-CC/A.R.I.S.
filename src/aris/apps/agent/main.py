import argparse
import json
import os
import random
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from aris.apps.agent.collector import collect
from aris.apps.agent.config import AgentSettings
from aris.apps.agent.schemas import AgentInspection
from aris.apps.agent.spool import Spool
from aris.modules.vega.schemas import GpuSample, Heartbeat, IngestResult


class CollectionFailed(RuntimeError):
    """Heartbeat delivered, but GPU collection failed; keep the heartbeat cadence."""


def flush(spool: Spool, settings: AgentSettings, client: httpx.Client) -> int:
    delivered = 0
    for batch in spool.pending():
        endpoint = "heartbeat" if isinstance(batch, Heartbeat) else "telemetry/batch"
        response = client.post(
            f"{settings.api_url.rstrip('/')}/api/agent/v1/{endpoint}",
            headers={"X-Agent-Token": settings.token.get_secret_value()},
            json=batch.model_dump(mode="json"),
        )
        response.raise_for_status()
        receipt = IngestResult.model_validate(response.json())
        if receipt.message_id != batch.message_id:
            raise ValueError("mismatched agent acknowledgement")
        spool.acknowledge(batch)
        delivered += 1
    return delivered


def tick(
    spool: Spool, settings: AgentSettings, client: httpx.Client, fixture: Path | None = None
) -> dict[str, int]:
    error: Exception | None = None
    collected = False
    try:
        samples = (
            TypeAdapter(list[GpuSample]).validate_json(fixture.read_text())
            if fixture
            else collect()
        )
        collected = True
        spool.enqueue(socket.gethostname(), samples)
    except Exception as exc:
        error = exc
    try:
        heartbeat = spool.enqueue_heartbeat(socket.gethostname(), "ok" if collected else "error")
    finally:
        # Even a full spool or a broken collector must allow the backlog to drain.
        delivered = flush(spool, settings, client)
    if error:
        if not collected:
            raise CollectionFailed(type(error).__name__) from None
        raise error
    return {"seq": heartbeat.seq, "delivered": delivered}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only NVIDIA GPU telemetry agent")
    parser.add_argument("--once", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--fixture", type=Path, help="Use explicit fixture GPU samples; no GPU needed"
    )
    source.add_argument(
        "--inspect",
        action="store_true",
        help="Print one real local GPU snapshot without credentials, network or spool",
    )
    args = parser.parse_args()
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
    failures = 0
    with httpx.Client(timeout=15, trust_env=False) as client:
        while True:
            try:
                result = tick(spool, settings, client, args.fixture)
                print(json.dumps(result), flush=True)
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
