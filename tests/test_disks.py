import json
import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aris.apps.agent.disk_collector import DiskCollector
from aris.apps.agent.disk_probe import probe_mounts, selected_volumes
from aris.modules.vega.disk_schemas import DiskObservation
from tests.conftest import READ_TOKEN
from tests.test_telemetry import batch, post

MOUNTS = r"""1 0 8:1 / / rw - ext4 /dev/sda1 rw
2 1 8:2 / /mnt/ssd rw - xfs /dev/nvme0n1 rw
3 1 8:2 / /mnt/alias rw - xfs /dev/nvme0n1 rw
4 1 8:3 / /home rw - ext4 /dev/sdb1 rw
5 1 0:9 / /mnt/ram rw - tmpfs tmpfs rw
6 1 0:10 / /mnt/auto rw - autofs automount rw
7 1 0:11 / /mnt/nas rw - nfs user:secret@host:/share rw,password=secret
8 1 8:4 / /mnt/data\040space rw - ext4 /dev/sdc1 rw
"""


def test_only_selected_mounts_are_probed_once_per_device_and_credentials_are_not_reported():
    volumes, truncated = selected_volumes(MOUNTS)
    assert not truncated and len(volumes) == 4
    assert volumes[0].mount_points == ["/"]
    assert next(v for v in volumes if v.device_id == "8:2").mount_points == [
        "/mnt/alias",
        "/mnt/ssd",
    ]
    assert "secret" not in json.dumps([v.model_dump() for v in volumes])
    assert any(v.mount_points == ["/mnt/data space"] for v in volumes)
    mapping = {path: v.device_id for v in volumes for path in v.mount_points}
    calls = []

    def stat(path):
        major, minor = map(int, mapping[path].split(":"))
        return SimpleNamespace(st_dev=os.makedev(major, minor))

    def usage(path):
        calls.append(path)
        if path == "/mnt/nas":
            raise PermissionError
        return SimpleNamespace(
            f_frsize=4096, f_bsize=4096, f_blocks=1000, f_bfree=300, f_bavail=250
        )

    result = probe_mounts(MOUNTS, stat=stat, statvfs=usage)
    assert result.quality == "partial"
    assert len(calls) == 4 and len(set(calls)) == 4
    assert "/home" not in calls and "/mnt/ram" not in calls and "/mnt/auto" not in calls
    assert "/mnt/ssd" not in calls  # Same device as the selected /mnt/alias.
    root = result.volumes[0]
    assert (root.total_bytes, root.used_bytes, root.available_bytes) == (
        1000 * 4096,
        700 * 4096,
        250 * 4096,
    )
    assert next(v for v in result.volumes if v.device_id == "0:11").total_bytes is None


def test_unmounted_or_replaced_mount_does_not_report_the_parent_filesystems_usage():
    result = probe_mounts(
        "1 0 8:1 / /mnt/data rw - ext4 /dev/sda1 rw",
        stat=lambda _: SimpleNamespace(st_dev=os.makedev(8, 9)),
        statvfs=lambda _: (_ for _ in ()).throw(AssertionError("must not query another device")),
    )
    assert result.quality == "error"
    assert result.volumes[0].error == "mount_changed"
    assert result.volumes[0].used_bytes is None


def clock():
    seconds = [0.0]
    start = datetime(2026, 11, 14, tzinfo=UTC)
    return seconds, lambda: seconds[0], lambda: start + timedelta(seconds=seconds[0])


def finish(collector):
    collector.thread.join(timeout=2)
    assert not collector.thread.is_alive()


def test_live_reads_do_not_block_or_reprobe_and_restart_preserves_full_hour_budget(tmp_path):
    seconds, monotonic, now = clock()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return DiskObservation(observed_at=now(), quality="unsupported")

    path = tmp_path / "disk.json"
    first = DiskCollector(path, probe=probe, clock=monotonic, now=now)
    try:
        assert first.sample() is None
        assert entered.wait(1)
        for _ in range(100):
            assert first.sample() is None
        assert len(calls) == 1
    finally:
        release.set()
        finish(first)
        first.close()
    stamp = first.latest.observed_at
    restarted = DiskCollector(path, probe=probe, clock=monotonic, now=now)
    seconds[0] = 3599
    assert restarted.sample().observed_at == stamp
    assert len(calls) == 1
    seconds[0] = 3600
    restarted.sample()
    finish(restarted)
    assert len(calls) == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_probe_is_not_retried_before_one_hour(tmp_path):
    seconds, monotonic, now = clock()
    calls = []

    def failed():
        calls.append(True)
        raise TimeoutError("private filesystem details")

    collector = DiskCollector(tmp_path / "disk.json", probe=failed, clock=monotonic, now=now)
    collector.sample()
    finish(collector)
    assert collector.sample().quality == "error"
    for seconds[0] in (1, 60, 1800, 3599):
        assert collector.sample().quality == "error"
    assert len(calls) == 1
    assert "private filesystem" not in collector.cache_path.read_text()


def test_crashed_inflight_record_waits_full_hour_after_restart_and_once_only_reads_cache(tmp_path):
    seconds, monotonic, now = clock()
    path = tmp_path / "disk.json"
    path.write_text(
        json.dumps(
            {
                "last_attempt_at": (now() - timedelta(days=1)).isoformat(),
                "last_finished_at": None,
                "observation": None,
            }
        )
    )
    calls = []

    def probe():
        calls.append(True)
        return DiskObservation(observed_at=now(), quality="unsupported")

    restarted = DiskCollector(path, probe=probe, clock=monotonic, now=now)
    for seconds[0] in (0, 3599):
        assert restarted.sample() is None
    assert calls == []
    seconds[0] = 3600
    restarted.sample()
    finish(restarted)
    assert len(calls) == 1
    seconds[0] = 10000
    once = DiskCollector(path, probe=probe, clock=monotonic, now=now, enabled=False)
    assert once.sample() is not None and len(calls) == 1


def test_invalid_cache_is_preserved_and_does_not_reset_hourly_limit(tmp_path):
    path = tmp_path / "disk.json"
    path.write_text("invalid private cache")
    collector = DiskCollector(path, probe=lambda: (_ for _ in ()).throw(AssertionError()))
    assert collector.cache_error
    assert collector.sample() is None and collector.thread is None
    assert path.read_text() == "invalid private cache"


def disk_observation(at, used=100):
    return DiskObservation.model_validate(
        {
            "observed_at": at,
            "quality": "ok",
            "volumes": [
                {
                    "device_id": "8:1",
                    "source": "/dev/sda1",
                    "fs_type": "ext4",
                    "mount_points": ["/"],
                    "quality": "ok",
                    "total_bytes": 1000,
                    "used_bytes": used,
                    "available_bytes": 700,
                }
            ],
        }
    )


def resource(client):
    response = client.get("/public/resources", headers={"Authorization": "Bearer " + READ_TOKEN})
    assert response.status_code == 200
    return response.json()["nodes"][0]


def test_fresh_gpu_does_not_refresh_or_regress_cached_disk_fact(client):
    observed = datetime.now(UTC) - timedelta(seconds=3500)
    payload = batch()
    payload["resources"]["disks"] = disk_observation(observed).model_dump(mode="json")
    assert post(client, payload).status_code == 200
    first = resource(client)
    assert first["disks_freshness"] == "CURRENT"
    original_stamp = first["disks_observed_at"]
    assert post(client, batch(seq=2)).status_code == 200  # Deployed baseline omits the field.
    assert resource(client)["disks_observed_at"] == original_stamp
    older = batch(seq=3)
    older["resources"]["disks"] = disk_observation(
        observed - timedelta(seconds=30), used=200
    ).model_dump(mode="json")
    assert post(client, older).status_code == 200
    current = resource(client)
    assert current["disks_observed_at"] == original_stamp
    assert current["disks"]["volumes"][0]["used_bytes"] == 100
    assert current["node"]["telemetry_status"] == "CURRENT"


@pytest.mark.parametrize("node_age,disk_age", [(0, 7201), (200, 200)])
def test_old_disk_or_offline_node_hides_values_even_if_other_component_is_fresh(
    client, node_age, disk_age
):
    payload = batch(seconds_ago=node_age)
    payload["resources"]["disks"] = disk_observation(
        datetime.fromisoformat(payload["observed_at"]) - timedelta(seconds=disk_age - node_age)
    ).model_dump(mode="json")
    assert post(client, payload).status_code == 200
    current = resource(client)
    assert current["disks_freshness"] == "STALE" and current["disks"] is None
    assert current["disks_observed_at"] is not None
    if node_age == 0:
        assert current["node"]["telemetry_status"] == "CURRENT"
