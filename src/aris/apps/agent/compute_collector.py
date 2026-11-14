import csv
import io
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime

from aris.apps.agent.system_collector import failed_quality
from aris.modules.vega.resource_schemas import ComputeObservation, GpuComputeSummary

COMPUTE_QUERY = [
    "nvidia-smi",
    "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
    "--format=csv,noheader,nounits",
]
UNKNOWN_MEMORY = {"N/A", "[N/A]", "[Not Supported]"}


def parse_compute_csv(output: str, inventory: Sequence[str]) -> ComputeObservation:
    # PID is transient parsing state, discarded before the contract is constructed.
    grouped: dict[str, dict[int, int | None]] = {gpu_uuid: {} for gpu_uuid in inventory}
    for row in csv.reader(io.StringIO(output), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 3:
            raise ValueError("unexpected compute columns")
        gpu_uuid, raw_pid, raw_memory = [part.strip() for part in row]
        if gpu_uuid not in grouped:
            raise ValueError("compute GPU is outside the current inventory")
        pid = int(raw_pid)
        memory = None if raw_memory in UNKNOWN_MEMORY else int(raw_memory)
        if pid <= 0 or (memory is not None and memory < 0):
            raise ValueError("invalid compute counter")
        previous = grouped[gpu_uuid]
        if pid in previous and previous[pid] != memory:
            raise ValueError("conflicting compute row")
        previous[pid] = memory
    return ComputeObservation(
        observed_at=datetime.now(UTC),
        quality="ok",
        reported_processes=len({pid for group in grouped.values() for pid in group}),
        gpus=[
            GpuComputeSummary(
                gpu_uuid=gpu_uuid,
                reported_processes=len(group),
                reported_memory_mib=(
                    None
                    if any(value is None for value in group.values())
                    else sum(value for value in group.values() if value is not None)
                ),
            )
            for gpu_uuid, group in sorted(grouped.items())
        ],
    )


class ComputeCollector:
    def collect(self, inventory: Sequence[str]) -> ComputeObservation:
        try:
            result = subprocess.run(
                COMPUTE_QUERY, capture_output=True, text=True, check=True, timeout=2
            )
            return parse_compute_csv(result.stdout, inventory)
        except Exception as exc:
            return ComputeObservation(observed_at=datetime.now(UTC), quality=failed_quality(exc))
