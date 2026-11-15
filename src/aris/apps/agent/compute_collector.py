import csv
import io
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime

from aris.apps.agent.process_users import ProcessUsers
from aris.apps.agent.system_collector import failed_quality
from aris.modules.vega.resource_schemas import (
    ComputeObservation,
    ComputeOwnership,
    GpuComputeSummary,
)

COMPUTE_QUERY = [
    "nvidia-smi",
    "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
    "--format=csv,noheader,nounits",
]
UNKNOWN_MEMORY = {"N/A", "[N/A]", "[Not Supported]"}


def parse_compute_csv(
    output: str, inventory: Sequence[str], *, users: ProcessUsers | None = None
) -> ComputeObservation:
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
    ownership = None
    if users:
        try:
            ownership = users.collect(grouped)
        except Exception:
            # Optional account observation cannot erase valid driver counters.
            ownership = ComputeOwnership(
                quality="partial",
                unresolved_processes=len({pid for group in grouped.values() for pid in group}),
            )
    return ComputeObservation(
        observed_at=datetime.now(UTC),
        quality="ok",
        user_scope=(
            "reported_linux_users"
            if ownership is not None and ownership.quality != "unsupported"
            else "unknown"
        ),
        ownership=ownership,
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
    def __init__(self, users: ProcessUsers | None = None) -> None:
        self.users = users or ProcessUsers()

    def collect(self, inventory: Sequence[str]) -> ComputeObservation:
        try:
            result = subprocess.run(
                COMPUTE_QUERY, capture_output=True, text=True, check=True, timeout=2
            )
            return parse_compute_csv(result.stdout, inventory, users=self.users)
        except Exception as exc:
            return ComputeObservation(observed_at=datetime.now(UTC), quality=failed_quality(exc))
