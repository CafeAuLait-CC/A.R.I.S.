"""A persistent hourly budget and an asynchronous, read-only disk probe."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aris.modules.vega.disk_schemas import DiskObservation

INTERVAL_SECONDS = 3600


def run_probe() -> DiskObservation:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "aris.apps.agent.disk_probe"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    if len(result.stdout) > 1048576:
        raise ValueError("disk probe output exceeds bounded size")
    return DiskObservation.model_validate_json(result.stdout)


class DiskCollector:
    def __init__(
        self,
        cache_path: Path,
        *,
        probe: Callable[[], DiskObservation] = run_probe,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        enabled: bool = True,
        emit: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.probe, self.clock, self.now = probe, clock, now
        self.enabled, self.emit = enabled, emit
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.closed = False
        self.cache_error = False
        self.latest: DiskObservation | None = None
        self.last_attempt: datetime | None = None
        self.next_due = float("-inf")
        try:
            if cache_path.is_symlink():
                raise ValueError("disk cache must not be a symlink")
            if cache_path.exists():
                with cache_path.open("rb") as stream:
                    raw = stream.read(1048577)
                if len(raw) > 1048576:
                    raise ValueError("disk cache exceeds bounded size")
                data = json.loads(raw)
                if set(data) != {"last_attempt_at", "last_finished_at", "observation"}:
                    raise ValueError("unsupported disk cache shape")
                attempted = datetime.fromisoformat(data["last_attempt_at"])
                if attempted.tzinfo is None or attempted.utcoffset() is None:
                    raise ValueError("disk attempt requires timezone")
                self.last_attempt = attempted.astimezone(UTC)
                if data["observation"] is not None:
                    self.latest = DiskObservation.model_validate(data["observation"])
                finished = data["last_finished_at"]
                if finished is None:
                    # A crash left an uncertain in-flight probe. Conservatively
                    # wait a whole hour after restart, rather than probing twice.
                    age = 0.0
                else:
                    completed = datetime.fromisoformat(finished)
                    if completed.tzinfo is None or completed.utcoffset() is None:
                        raise ValueError("disk completion requires timezone")
                    if completed < self.last_attempt:
                        raise ValueError("disk completion precedes attempt")
                    age = (self.now() - completed).total_seconds()
                self.next_due = self.clock() + max(0, INTERVAL_SECONDS - age)
        except (OSError, ValueError, TypeError, KeyError):
            # Do not erase an invalid rate-limit record and immediately re-probe.
            self.cache_error = True

    def _persist(
        self,
        attempted: datetime,
        observation: DiskObservation | None,
        finished: datetime | None = None,
    ) -> None:
        path = self.cache_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=path.name + ".", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    {
                        "last_attempt_at": attempted.isoformat(),
                        "last_finished_at": finished.isoformat() if finished else None,
                        "observation": observation.model_dump(mode="json") if observation else None,
                    },
                    stream,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def sample(self) -> DiskObservation | None:
        with self.lock:
            if (
                self.enabled
                and not self.closed
                and not self.cache_error
                and (self.thread is None or not self.thread.is_alive())
                and self.clock() >= self.next_due
            ):
                self.next_due = self.clock() + INTERVAL_SECONDS
                self.thread = threading.Thread(target=self._collect, name="vega-disks", daemon=True)
                self.thread.start()
            # A clock rollback must not poison the entire GPU envelope with an
            # hourly cached observation that follows the new send time.
            if self.latest is not None and self.latest.observed_at <= self.now():
                return self.latest
            return None

    def _collect(self) -> None:
        attempted = self.now()
        error_type: str | None = None
        try:
            # This durable attempt precedes all statvfs calls, including failures.
            self._persist(attempted, self.latest)
        except OSError as exc:
            with self.lock:
                self.cache_error = True
            error_type = type(exc).__name__
            observation = None
        else:
            with self.lock:
                self.last_attempt = attempted
            try:
                observation = self.probe()
            except Exception as exc:
                error_type = type(exc).__name__
                observation = DiskObservation(observed_at=self.now(), quality="error")
            with self.lock:
                self.latest = observation
            try:
                self._persist(attempted, observation, max(attempted, self.now()))
            except OSError as exc:
                with self.lock:
                    self.cache_error = True
                error_type = type(exc).__name__
            with self.lock:
                self.next_due = self.clock() + INTERVAL_SECONDS
        if self.emit:
            self.emit(
                {
                    "phase": "disk_probe",
                    "cache_error": self.cache_error,
                    "quality": observation.quality if observation else "unknown",
                    "error_type": error_type,
                }
            )

    def close(self) -> None:
        with self.lock:
            self.closed = True
        if self.thread:
            self.thread.join(timeout=0.2)
