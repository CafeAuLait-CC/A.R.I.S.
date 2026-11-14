import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from aris.modules.vega.resource_schemas import CpuObservation, MemoryObservation, SystemObservation


def failed_quality(exc: Exception) -> Literal["unsupported", "error"]:
    return "unsupported" if isinstance(exc, FileNotFoundError) else "error"


class SystemCollector:
    """Read aggregate counters only; never open /proc/PID or writable sysctl files."""

    def __init__(
        self, root: Path = Path("/proc"), clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.root = root
        self.clock = clock
        self.previous: tuple[tuple[int, ...], float, int | None] | None = None

    def cpu(self) -> CpuObservation:
        try:
            lines = (self.root / "stat").read_text().splitlines()
            fields = lines[0].split()
            if fields[0] != "cpu" or len(fields) < 9:
                raise ValueError("aggregate CPU counters missing")
            # Guest time is already accounted in user/nice; don't add the last two columns.
            counters = tuple(int(value) for value in fields[1:9])
            if any(value < 0 for value in counters):
                raise ValueError("negative CPU counter")
            logical = (
                sum(
                    1
                    for line in lines
                    if line.split()
                    and line.split()[0][3:].isdigit()
                    and line.split()[0].startswith("cpu")
                )
                or None
            )
            now = self.clock()
            previous = self.previous
            self.previous = counters, now, logical
            if previous is not None and previous[2] == logical:
                delta = [new - old for new, old in zip(counters, previous[0], strict=True)]
                total = sum(delta)
                window = now - previous[1]
                # Counter resets, hotplug and decreasing iowait need a new baseline.
                if all(value >= 0 for value in delta) and total > 0 and window > 0:
                    idle = delta[3] + delta[4]
                    return CpuObservation(
                        quality="ok",
                        logical_count=logical,
                        utilization_percent=round(100 * (total - idle) / total, 2),
                        window_seconds=window,
                    )
            return CpuObservation(quality="warming", logical_count=logical)
        except Exception as exc:
            self.previous = None
            return CpuObservation(quality=failed_quality(exc))

    def memory(self) -> MemoryObservation:
        try:
            values = {}
            for line in (self.root / "meminfo").read_text().splitlines():
                name, _, raw = line.partition(":")
                if name in {"MemTotal", "MemAvailable"}:
                    parts = raw.split()
                    if len(parts) != 2 or parts[1] != "kB":
                        raise ValueError("unexpected memory unit")
                    values[name] = int(parts[0]) * 1024
            if set(values) != {"MemTotal", "MemAvailable"}:
                return MemoryObservation(quality="unsupported")
            return MemoryObservation(
                quality="ok", total_bytes=values["MemTotal"], available_bytes=values["MemAvailable"]
            )
        except Exception as exc:
            return MemoryObservation(quality=failed_quality(exc))

    def collect(self) -> SystemObservation:
        cpu = self.cpu()
        memory = self.memory()
        return SystemObservation(observed_at=datetime.now(UTC), cpu=cpu, memory=memory)
