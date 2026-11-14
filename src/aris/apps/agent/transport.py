"""Bounded delivery that cannot hold the hardware sampling loop in an HTTP retry."""

import json
import random
import threading
import time
from collections.abc import Callable

import httpx

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.spool import Spool
from aris.modules.vega.schemas import Heartbeat, IngestResult

_REPORT_LOCK = threading.Lock()


def flush(
    spool: Spool,
    settings: AgentSettings,
    client: httpx.Client,
    *,
    clock: Callable[[], float] = time.monotonic,
    delivered_sequences: list[int] | None = None,
) -> int:
    delivered = 0
    started = clock()
    for batch in spool.delivery_batch(settings.flush_max_messages):
        endpoint = "heartbeat" if isinstance(batch, Heartbeat) else "telemetry/batch"
        response = client.post(
            f"{settings.api_url.rstrip('/')}/vega/{endpoint}",
            headers={"X-Agent-Token": settings.token.get_secret_value()},
            json=batch.model_dump(mode="json"),
        )
        response.raise_for_status()
        receipt = IngestResult.model_validate(response.json())
        if receipt.message_id != batch.message_id:
            raise ValueError("mismatched agent acknowledgement")
        spool.acknowledge(batch)
        delivered += 1
        if delivered_sequences is not None:
            delivered_sequences.append(batch.seq)
        if delivered >= 3 and clock() - started >= settings.flush_budget_seconds:
            break
    return delivered


def report(value: dict[str, object]) -> None:
    with _REPORT_LOCK:
        print(json.dumps(value), flush=True)


def retry_delay(failures: int) -> float:
    return min(60.0, 2.0 ** min(failures, 6)) + random.uniform(0, 1)


class DeliveryWorker:
    """Exactly one sender per Collector; SQLite transactions never span HTTP calls."""

    def __init__(
        self,
        spool: Spool,
        settings: AgentSettings,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        emit: Callable[[dict[str, object]], None] = report,
        backoff: Callable[[int], float] = retry_delay,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spool = spool
        self.settings = settings
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=15, trust_env=False))
        self.emit = emit
        self.backoff = backoff
        self.clock = clock
        self.stopped = threading.Event()
        self.changed = threading.Event()
        self.thread = threading.Thread(target=self.run, name="vega-delivery", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def wake(self) -> None:
        self.changed.set()

    def close(self) -> None:
        self.stopped.set()
        self.changed.set()
        self.thread.join(timeout=2)

    def run(self) -> None:
        failures = 0
        next_attempt = 0.0
        try:
            with self.client_factory() as client:
                while not self.stopped.is_set():
                    remaining = next_attempt - self.clock()
                    stats = self.spool.statistics()
                    if remaining > 0 or not (
                        stats["pending_batches"] + stats["pending_heartbeats"]
                    ):
                        self.changed.wait(remaining if remaining > 0 else 30)
                        self.changed.clear()
                        continue
                    sequences: list[int] = []
                    error_type: str | None = None
                    http_status: int | None = None
                    try:
                        flush(self.spool, self.settings, client, delivered_sequences=sequences)
                    except Exception as exc:
                        error_type = type(exc).__name__
                        if isinstance(exc, httpx.HTTPStatusError):
                            http_status = exc.response.status_code
                        failures += 1
                        next_attempt = self.clock() + self.backoff(failures)
                    else:
                        failures = 0
                        next_attempt = self.clock()
                    self.emit(
                        {
                            "phase": "delivery",
                            "delivered": len(sequences),
                            "acknowledged_sequences": sequences,
                            "error_type": error_type,
                            "http_status": http_status,
                            "retry_in_seconds": max(0, next_attempt - self.clock()),
                            "buffer": self.spool.statistics(),
                        }
                    )
        except Exception as exc:
            self.emit({"phase": "delivery_stopped", "error_type": type(exc).__name__})
