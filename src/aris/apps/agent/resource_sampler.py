import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from aris.apps.agent.collector import collect
from aris.apps.agent.compute_collector import ComputeCollector
from aris.apps.agent.system_collector import SystemCollector, failed_quality
from aris.modules.vega.resource_schemas import (
    ComputeObservation,
    GpuObservation,
    ResourceObservation,
)
from aris.modules.vega.schemas import GpuSample


class ResourceSampler:
    """Shared GPU/system sampling; compute summary keeps its own slower timestamp."""

    def __init__(
        self,
        gpu: Callable[[], list[GpuSample]] = collect,
        system: SystemCollector | None = None,
        compute: ComputeCollector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gpu = gpu
        self.system = system or SystemCollector(clock=clock)
        self.compute = compute or ComputeCollector()
        self.clock = clock
        self.lock = threading.Lock()
        self.latest: ResourceObservation | None = None
        self.last_sample = float("-inf")
        self.last_compute = float("-inf")
        self.inventory: tuple[str, ...] = ()
        self.compute_observation = ComputeObservation(
            observed_at=datetime.now(UTC), quality="unknown"
        )

    def sample(self, *, fresh_after: datetime | None = None) -> ResourceObservation:
        with self.lock:
            if (
                self.latest is not None
                and self.clock() - self.last_sample < 0.75
                and (fresh_after is None or self.latest.gpu.observed_at > fresh_after)
            ):
                return self.latest
            try:
                samples = self.gpu()
                gpu = GpuObservation(observed_at=datetime.now(UTC), quality="ok", gpus=samples)
            except Exception as exc:
                gpu = GpuObservation(observed_at=datetime.now(UTC), quality=failed_quality(exc))
            system = self.system.collect()
            inventory = (
                tuple(sorted(item.gpu_uuid for item in gpu.gpus))
                if gpu.quality == "ok"
                else self.inventory
            )
            if gpu.quality == "unsupported":
                self.compute_observation = ComputeObservation(
                    observed_at=gpu.observed_at, quality="unsupported"
                )
            elif self.clock() - self.last_compute >= 15 or inventory != self.inventory:
                self.compute_observation = self.compute.collect(inventory)
                self.last_compute = self.clock()
            self.inventory = inventory
            self.latest = ResourceObservation(
                observed_at=max(
                    gpu.observed_at, system.observed_at, self.compute_observation.observed_at
                ),
                gpu=gpu,
                system=system,
                compute=self.compute_observation,
            )
            self.last_sample = self.clock()
            return self.latest
