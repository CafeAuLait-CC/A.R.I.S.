import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aris.apps.agent.compute_collector import COMPUTE_QUERY, ComputeCollector, parse_compute_csv
from aris.apps.agent.main import main
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.system_collector import SystemCollector
from aris.modules.vega.resource_schemas import CpuObservation, MemoryObservation
from tests.test_agent import samples


def proc(
    root,
    cpu="100 0 100 800 0 0 0 0 20 0",
    logical=2,
    memory="MemTotal: 4096 kB\nMemAvailable: 1024 kB\n",
):
    root.mkdir(exist_ok=True)
    (root / "stat").write_text(
        "cpu " + cpu + "\n" + "".join(f"cpu{i} {cpu}\n" for i in range(logical))
    )
    (root / "meminfo").write_text(memory)


def test_cpu_measures_an_interval_excludes_guest_and_memory_uses_available(tmp_path):
    proc(tmp_path)
    now = [10.0]
    collector = SystemCollector(tmp_path, clock=lambda: now[0])
    first = collector.collect()
    assert first.cpu.quality == "warming" and first.cpu.utilization_percent is None
    assert first.cpu.logical_count == 2
    assert first.memory.total_bytes == 4096 * 1024
    assert first.memory.available_bytes == 1024 * 1024
    now[0] += 2
    proc(tmp_path, cpu="120 0 120 860 0 0 0 0 999999 999999")
    second = collector.collect()
    assert second.cpu.quality == "ok"
    assert second.cpu.utilization_percent == 40
    assert second.cpu.window_seconds == 2


def test_cpu_counter_reset_and_hotplug_warm_up_without_fake_idle(tmp_path):
    now = [1.0]
    proc(tmp_path, cpu="100 0 100 800 10 0 0 0 0 0")
    collector = SystemCollector(tmp_path, clock=lambda: now[0])
    collector.collect()
    now[0] += 1
    proc(tmp_path, cpu="120 0 120 860 9 0 0 0 0 0")
    assert collector.collect().cpu.quality == "warming"  # iowait can decrease.
    now[0] += 1
    proc(tmp_path, cpu="140 0 140 920 9 0 0 0 0 0", logical=3)
    hotplug = collector.collect()
    assert hotplug.cpu.quality == "warming"
    assert hotplug.cpu.utilization_percent is None
    now[0] += 1
    proc(tmp_path, cpu="160 0 160 980 9 0 0 0 0 0", logical=3)
    assert collector.collect().cpu.utilization_percent == 40


def test_missing_or_broken_cpu_and_memory_are_independent_unknowns(tmp_path):
    collector = SystemCollector(tmp_path)
    missing = collector.collect()
    assert missing.cpu.quality == missing.memory.quality == "unsupported"
    proc(tmp_path)
    (tmp_path / "stat").write_text("private malformed counter")
    failed = collector.collect()
    assert failed.cpu.quality == "error" and failed.cpu.utilization_percent is None
    assert failed.memory.quality == "ok"
    assert "private" not in failed.model_dump_json()


@pytest.mark.parametrize(
    "memory,quality",
    [
        ("MemTotal: 4096 kB\n", "unsupported"),
        ("MemTotal: 4096 MB\nMemAvailable: 1 MB\n", "error"),
        ("MemTotal: 4096 kB\nMemAvailable: -1 kB\n", "error"),
        ("MemTotal: 4096 kB\nMemAvailable: 5000 kB\n", "error"),
    ],
)
def test_memory_never_substitutes_free_or_zero_for_unavailable(tmp_path, memory, quality):
    proc(tmp_path, memory=memory)
    result = SystemCollector(tmp_path).collect().memory
    assert result.quality == quality
    assert result.total_bytes is None and result.available_bytes is None


def test_unknown_contracts_reject_known_numbers():
    with pytest.raises(ValidationError):
        CpuObservation(quality="error", utilization_percent=0)
    with pytest.raises(ValidationError):
        MemoryObservation(quality="unsupported", total_bytes=1, available_bytes=0)


def test_compute_summary_discards_pid_deduplicates_and_preserves_unknown_memory():
    result = parse_compute_csv(
        "GPU-a, 123456, 64\nGPU-a,123456,64\nGPU-b,123456,32\nGPU-b,818181,N/A\n",
        ["GPU-a", "GPU-b"],
    )
    assert result.reported_processes == 2  # One process can use multiple GPUs.
    assert result.gpus[0].reported_processes == 1
    assert result.gpus[0].reported_memory_mib == 64
    assert result.gpus[1].reported_processes == 2
    assert result.gpus[1].reported_memory_mib is None
    body = result.model_dump_json()
    assert "123456" not in body and "818181" not in body and '"pid"' not in body
    assert result.user_scope == "unknown"
    assert result.scope == "driver_reported_compute_apps"


def test_empty_compute_query_is_zero_only_within_the_reported_compute_scope():
    result = parse_compute_csv("", ["GPU-a", "GPU-b"])
    assert result.quality == "ok" and result.reported_processes == 0
    assert all(gpu.reported_processes == 0 and gpu.reported_memory_mib == 0 for gpu in result.gpus)


@pytest.mark.parametrize(
    "raw",
    [
        "GPU-a,1",
        "GPU-other,1,32",
        "GPU-a,0,32",
        "GPU-a,1,-1",
        "GPU-a,1,32\nGPU-a,1,64",
        "GPU-a,N/A,32",
    ],
)
def test_compute_invalid_or_unattributable_rows_do_not_become_empty(raw):
    with pytest.raises(ValueError):
        parse_compute_csv(raw, ["GPU-a"])


def test_compute_uses_only_fixed_fields_and_sanitizes_driver_failure(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.CalledProcessError(1, command, output="private command path")

    monkeypatch.setattr("aris.apps.agent.compute_collector.subprocess.run", run)
    result = ComputeCollector().collect(["GPU-a"])
    assert result.quality == "error" and result.reported_processes is None and result.gpus == []
    assert calls[0][0] == COMPUTE_QUERY
    assert calls[0][1]["timeout"] == 2 and "shell" not in calls[0][1]
    assert "private" not in result.model_dump_json()


def test_resource_cache_reuses_compute_summary_without_faking_its_timestamp(tmp_path, monkeypatch):
    now = [0.0]
    proc(tmp_path)
    calls = []
    compute = ComputeCollector()

    def query(inventory):
        calls.append(tuple(inventory))
        return parse_compute_csv("", inventory)

    monkeypatch.setattr(compute, "collect", query)
    sampler = ResourceSampler(
        samples, SystemCollector(tmp_path, clock=lambda: now[0]), compute, clock=lambda: now[0]
    )
    first = sampler.sample()
    now[0] += 1
    second = sampler.sample(fresh_after=first.gpu.observed_at)
    assert second.gpu.observed_at > first.gpu.observed_at
    assert second.compute.observed_at == first.compute.observed_at
    assert len(calls) == 1

    def gpu_failed():
        raise RuntimeError("private GPU detail")

    sampler.gpu = gpu_failed
    now[0] += 1
    degraded = sampler.sample()
    assert degraded.gpu.quality == "error" and degraded.gpu.gpus == []
    assert degraded.system.memory.quality == "ok"
    assert "private" not in degraded.model_dump_json()
    now[0] += 16
    sampler.gpu = samples
    resumed = sampler.sample()
    assert len(calls) == 2
    assert resumed.compute.observed_at > first.compute.observed_at


def test_full_inspection_needs_no_credentials_network_or_spool(tmp_path, monkeypatch, capsys):
    proc(tmp_path)
    compute = ComputeCollector()
    monkeypatch.setattr(compute, "collect", lambda inventory: parse_compute_csv("", inventory))
    observation = ResourceSampler(samples, SystemCollector(tmp_path), compute).sample()

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not open credentials or transport")

    monkeypatch.setattr("sys.argv", ["aris-vega-collector", "--inspect"])
    monkeypatch.setattr(
        "aris.apps.agent.main.ResourceSampler", lambda: SimpleNamespace(sample=lambda: observation)
    )
    for name in ["AgentSettings", "Spool", "LiveClient", "httpx.Client"]:
        monkeypatch.setattr("aris.apps.agent.main." + name, forbidden)
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["resources"]["system"]["cpu"]["quality"] == "warming"
    assert result["resources"]["system"]["memory"]["quality"] == "ok"
    assert datetime.fromisoformat(result["resources"]["observed_at"]).tzinfo is UTC
