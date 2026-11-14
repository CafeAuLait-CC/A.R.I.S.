import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aris.modules.vega.schemas import GpuSample


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    gpus: list[GpuSample]


class Sampler:
    """Serialize driver reads and share a sub-second sample with the archive loop."""

    def __init__(
        self, collect: Callable[[], list[GpuSample]], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.collect = collect
        self.clock = clock
        self.lock = threading.Lock()
        self.last_attempt = float("-inf")
        self.latest: Observation | None = None

    def sample(self, *, fresh_after: datetime | None = None) -> Observation:
        with self.lock:
            needs_new_observation = fresh_after is not None and (
                self.latest is None or self.latest.observed_at <= fresh_after
            )
            if needs_new_observation or self.clock() - self.last_attempt >= 0.75:
                self.latest = None
                try:
                    gpus = self.collect()
                    self.latest = Observation(datetime.now(UTC), gpus)
                finally:
                    self.last_attempt = self.clock()
            if self.latest is None:
                raise RuntimeError("collection failed")
            return self.latest
