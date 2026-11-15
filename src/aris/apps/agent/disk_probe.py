"""One read-only filesystem probe; invoked only by the hourly cache worker."""

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aris.modules.vega.disk_schemas import DiskObservation, DiskVolume

PSEUDO_FILESYSTEMS = {
    "proc",
    "sysfs",
    "devtmpfs",
    "devpts",
    "tmpfs",
    "ramfs",
    "autofs",
    "cgroup",
    "cgroup2",
    "mqueue",
    "securityfs",
    "debugfs",
    "tracefs",
}


def unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def selected_volumes(mountinfo: str) -> tuple[list[DiskVolume], bool]:
    groups: dict[str, DiskVolume] = {}
    truncated = False
    for line in mountinfo.splitlines():
        try:
            left, right = line.split(" - ", 1)
            fields, description = left.split(), right.split()
            mount = unescape(fields[4])
            if mount != "/" and not mount.startswith("/mnt/"):
                continue
            fs_type, raw_source = description[:2]
            if mount != "/" and fs_type in PSEUDO_FILESYSTEMS:
                continue
            device = fields[2]
            # Sources such as network URLs and mount options can contain credentials.
            # Only conventional /dev paths leave this helper; otherwise use the ID.
            source = unescape(raw_source)
            if not re.fullmatch(r"/dev/[A-Za-z0-9_./+\-]+", source):
                source = f"{fs_type} [{device}]"
            volume = DiskVolume(
                device_id=device,
                source=source,
                fs_type=fs_type,
                mount_points=[mount],
                quality="error",
                error="unavailable",
            )
            if device in groups:
                mounts = groups[device].mount_points
                if mount not in mounts:
                    if len(mounts) < 64:
                        mounts.append(mount)
                    else:
                        truncated = True
            else:
                groups[device] = volume
        except (ValueError, IndexError):
            truncated = True
    volumes = sorted(groups.values(), key=lambda v: ("/" not in v.mount_points, v.mount_points[0]))
    for volume in volumes:
        volume.mount_points.sort(key=lambda value: (value != "/", value))
    selected: list[DiskVolume] = []
    remaining = 15000
    for volume in volumes[:64]:
        while (
            len(volume.model_dump_json().encode()) + 256 > remaining
            and len(volume.mount_points) > 1
        ):
            volume.mount_points.pop()
            truncated = True
        size = len(volume.model_dump_json().encode()) + 256  # Room for actual counters.
        if size > remaining:
            truncated = True
            continue
        remaining -= size
        selected.append(volume)
    return selected, truncated or len(volumes) > 64


def probe_mounts(
    mountinfo: str,
    *,
    stat: Callable[[str], Any] = os.stat,
    statvfs: Callable[[str], Any] = os.statvfs,
) -> DiskObservation:
    volumes, truncated = selected_volumes(mountinfo)
    results: list[DiskVolume] = []
    for volume in volumes:
        mount = volume.mount_points[0]
        expected = tuple(int(part) for part in volume.device_id.split(":"))
        try:
            before = stat(mount).st_dev
            if (os.major(before), os.minor(before)) != expected:
                results.append(volume.model_copy(update={"error": "mount_changed"}))
                continue
            counters = statvfs(mount)
            if stat(mount).st_dev != before:
                results.append(volume.model_copy(update={"error": "mount_changed"}))
                continue
            block_size = counters.f_frsize or counters.f_bsize
            total = counters.f_blocks * block_size
            free = counters.f_bfree * block_size
            available = max(0, counters.f_bavail * block_size)
            results.append(
                DiskVolume(
                    **{
                        **volume.model_dump(),
                        "quality": "ok",
                        "error": None,
                        "total_bytes": total,
                        "used_bytes": total - free,
                        "available_bytes": available,
                    }
                )
            )
        except OSError:
            results.append(volume)
        except ValueError:
            results.append(volume.model_copy(update={"error": "invalid_counters"}))
    quality = "unsupported" if not results and not truncated else "error"
    if any(volume.quality == "ok" for volume in results):
        quality = (
            "ok"
            if all(volume.quality == "ok" for volume in results) and not truncated
            else "partial"
        )
    return DiskObservation(
        observed_at=datetime.now(UTC), quality=quality, volumes=results, truncated=truncated
    )


def probe() -> DiskObservation:
    try:
        with Path("/proc/self/mountinfo").open() as stream:
            mountinfo = stream.read(1048577)
        if len(mountinfo) > 1048576:
            raise ValueError("mount inventory exceeds bounded probe size")
        return probe_mounts(mountinfo)
    except FileNotFoundError:
        return DiskObservation(observed_at=datetime.now(UTC), quality="unsupported")
    except (OSError, ValueError):
        return DiskObservation(observed_at=datetime.now(UTC), quality="error")


if __name__ == "__main__":
    print(probe().model_dump_json())
