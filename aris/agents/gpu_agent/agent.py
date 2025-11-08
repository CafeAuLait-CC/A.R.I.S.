import time
import subprocess
import requests
from datetime import datetime

ARIS_ENDPOINT = "https://your-host/internal/gpu/report"
NODE_NAME = "ubco"
NODE_TOKEN = "your_node_token"


def get_gpu_processes():
    # simplified. use nvidia-smi --query-compute-apps to get detailed data
    # placeholder
    return [
        {
            "gpu_index": 0,
            "username": "alice",
            "process_count": 2,
            "memory_used_mb": 8000,
        }
    ]


def loop():
    while True:
        payload = {
            "node_name": NODE_NAME,
            "token": NODE_TOKEN,
            "timestamp": datetime.utcnow().isoformat(),
            "processes": get_gpu_processes(),
        }
        try:
            requests.post(ARIS_ENDPOINT, json=payload, timeout=3)
        except Exception:
            pass

        time.sleep(30)


if __name__ == "__main__":
    loop()
