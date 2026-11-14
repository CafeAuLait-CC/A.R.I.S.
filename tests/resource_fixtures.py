from datetime import UTC, datetime, timedelta

from aris.apps.agent.collector import parse_nvidia_csv
from aris.modules.vega.resource_schemas import ResourceObservation


def samples():
    return parse_nvidia_csv("GPU-a, 0, NVIDIA RTX A6000, 49140, 0, 0, N/A\n")


def resource_observation(
    seconds_ago=0,
    gpu_quality="ok",
    cpu_quality="ok",
    compute_age=0,
    utilization=40,
    gpus=None,
    observed_at=None,
):
    observed = observed_at or datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return ResourceObservation.model_validate(
        {
            "observed_at": observed,
            "gpu": {
                "observed_at": observed,
                "quality": gpu_quality,
                "gpus": (samples() if gpus is None else gpus) if gpu_quality == "ok" else [],
            },
            "system": {
                "observed_at": observed,
                "cpu": {
                    "quality": cpu_quality,
                    "logical_count": 8,
                    "utilization_percent": utilization if cpu_quality == "ok" else None,
                    "window_seconds": 15 if cpu_quality == "ok" else None,
                },
                "memory": {"quality": "ok", "total_bytes": 16384, "available_bytes": 4096},
            },
            "compute": {
                "observed_at": observed - timedelta(seconds=compute_age),
                "quality": "ok",
                "reported_processes": 1,
                "gpus": [
                    {"gpu_uuid": "GPU-a", "reported_processes": 1, "reported_memory_mib": None}
                ],
            },
        }
    )
