"""Derive activity from measured facts without treating driver memory as a job."""

from typing import Literal

ObservedGpuState = Literal["idle", "busy", "unknown"]
IDLE_DRIVER_MEMORY_MIB = 64


def gpu_activity(
    memory_used_mib: int, utilization_percent: int, reported_processes: int | None
) -> ObservedGpuState:
    if (
        utilization_percent > 0
        or memory_used_mib > IDLE_DRIVER_MEMORY_MIB
        or (reported_processes is not None and reported_processes > 0)
    ):
        return "busy"
    # Unknown/stale process visibility is not evidence of zero processes.
    return "idle" if reported_processes == 0 else "unknown"
