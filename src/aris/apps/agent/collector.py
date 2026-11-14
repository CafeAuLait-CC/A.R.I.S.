import csv
import io
import subprocess

from aris.modules.vega.schemas import GpuSample

GPU_QUERY = [
    "nvidia-smi",
    "--query-gpu=uuid,index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
    "--format=csv,noheader,nounits",
]


def parse_nvidia_csv(output: str) -> list[GpuSample]:
    result: list[GpuSample] = []
    for row in csv.reader(io.StringIO(output), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 7:
            raise ValueError("unexpected nvidia-smi columns")
        uuid, index, model, total, used, utilization, temperature = [part.strip() for part in row]
        result.append(
            GpuSample(
                gpu_uuid=uuid,
                index=int(index),
                model=model,
                memory_total_mib=int(total),
                memory_used_mib=int(used),
                utilization_percent=int(utilization),
                temperature_c=None
                if temperature in {"N/A", "[N/A]", "[Not Supported]"}
                else int(temperature),
            )
        )
    if not result:
        raise ValueError("nvidia-smi reported no GPUs; explicit inventory reconciliation required")
    return result


def collect() -> list[GpuSample]:
    completed = subprocess.run(GPU_QUERY, capture_output=True, text=True, check=True, timeout=10)
    return parse_nvidia_csv(completed.stdout)
