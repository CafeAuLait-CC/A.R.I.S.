"""Best-effort node-local UID summaries; no PID or account authentication leaves this module."""

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from aris.modules.vega.resource_schemas import (
    ComputeOwnership,
    ComputeUserSummary,
    GpuComputeSummary,
)

ProcessInventory = Mapping[str, Mapping[int, int | None]]

_LOOKUP_NAMES = """
import json, pwd, sys
result = {}
for uid in json.load(sys.stdin):
    try:
        name = pwd.getpwuid(uid).pw_name
        if 0 < len(name) <= 128 and all(ord(c) >= 32 and ord(c) != 127 for c in name):
            result[str(uid)] = name
    except KeyError:
        pass
print(json.dumps(result))
"""


def lookup_names(uids: list[int]) -> dict[int, str]:
    if not uids:
        return {}
    try:
        # NSS may consult a remote directory. Bound its effect on sampling;
        # the program and arguments are fixed, and stdin contains only UID integers.
        result = subprocess.run(
            [sys.executable, "-I", "-c", _LOOKUP_NAMES],
            input=json.dumps(uids),
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        )
        names = json.loads(result.stdout)
        return {
            uid: names[str(uid)]
            for uid in uids
            if isinstance(names, dict) and isinstance(names.get(str(uid)), str)
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


class ProcessUsers:
    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        names: Callable[[list[int]], dict[int, str]] = lookup_names,
    ) -> None:
        self.proc_root = proc_root
        self.names = names

    def collect(self, inventory: ProcessInventory) -> ComputeOwnership:
        pids = {pid for group in inventory.values() for pid in group}
        if not self.proc_root.is_dir():
            return ComputeOwnership(quality="unsupported", unresolved_processes=len(pids))
        owners: dict[int, int] = {}
        for pid in pids:
            try:
                directory = self.proc_root / str(pid)
                uid = directory.stat().st_uid
                # Non-dumpable tasks can expose root-owned proc entries. Check
                # effective UID metadata instead of attributing those tasks to root.
                with (directory / "status").open() as status:
                    fields = next(line.split() for line in status if line.startswith("Uid:"))
                if len(fields) != 5 or int(fields[2]) != uid or directory.stat().st_uid != uid:
                    continue
                owners[pid] = uid
            except (OSError, ValueError, StopIteration):
                # Ended, inaccessible or hidden process: keep it unresolved.
                continue
        # Bound the number of NSS names and wire groups without inventing owners
        # for overflow. Counts always refer to the original driver-reported set.
        uids = sorted(set(owners.values()))[:256]
        names = self.names(uids)
        users: list[ComputeUserSummary] = []
        wire_bytes = 0
        for uid in uids:
            owned = {pid for pid, candidate in owners.items() if candidate == uid}
            gpus: list[GpuComputeSummary] = []
            for gpu, group in sorted(inventory.items()):
                memory = [value for pid, value in group.items() if pid in owned]
                if memory:
                    gpus.append(
                        GpuComputeSummary(
                            gpu_uuid=gpu,
                            reported_processes=len(memory),
                            reported_memory_mib=(
                                None
                                if any(value is None for value in memory)
                                else sum(value for value in memory if value is not None)
                            ),
                        )
                    )
            user = ComputeUserSummary(
                uid=uid, username=names.get(uid), reported_processes=len(owned), gpus=gpus
            )
            encoded_bytes = len(user.model_dump_json().encode())
            if wire_bytes + encoded_bytes > 16384:
                break
            wire_bytes += encoded_bytes
            users.append(user)
        unresolved = len(pids) - sum(user.reported_processes for user in users)
        complete = unresolved == 0 and all(user.username is not None for user in users)
        return ComputeOwnership(
            quality="ok" if complete else "partial",
            users=users,
            unresolved_processes=unresolved,
        )
