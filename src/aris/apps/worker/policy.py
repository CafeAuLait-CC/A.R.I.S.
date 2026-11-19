"""Manually run the existing Core worker for confirmed policy-draft intents."""

import json
import signal
from threading import Event
from time import monotonic

from aris.core.commands.worker import CommandWorker


def run_policy_draft_worker(worker: CommandWorker, *, once: bool, poll_seconds: float) -> None:
    if once:
        result = worker.run_once()
        print(json.dumps(result.model_dump(mode="json") if result else {"state": "idle"}))
        return
    stopping = Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    last_idle = float("-inf")
    try:
        while not stopping.is_set():
            result = worker.run_once()
            now = monotonic()
            if result is not None:
                print(json.dumps(result.model_dump(mode="json")), flush=True)
                last_idle = now
            elif now - last_idle >= 900:
                print(json.dumps({"state": "idle", "mode": "policy-draft-worker"}), flush=True)
                last_idle = now
            stopping.wait(poll_seconds)
    except KeyboardInterrupt:
        return
    finally:
        signal.signal(signal.SIGTERM, previous)
