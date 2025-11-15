import datetime as dt
import logging
import os
import pwd
import re
import subprocess
import time

import pytz
import requests
from dotenv import load_dotenv

# ==== Basic Configuration ====

load_dotenv()

AGENT_VERSION = "0.1.0"
GATEWAY_URL = os.getenv("ARIS_GATEWAY_URL", "http://hostname")
BASE_PATH = os.getenv("GPU_AGENT_BASE_PATH", "/internal/gpu")
AGENT_TOKEN = os.getenv("GPU_AGENT_TOKEN", "")
POLL_SECS = int(os.getenv("GPU_AGENT_POLL_SECS", "10"))
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
HOSTNAME = os.getenv("GPU_AGENT_HOSTNAME") or os.uname().nodename
AGENT_LOG = os.getenv("GPU_AGENT_LOG", "./GPU_AGENT_LOG.log")

session_timeout_secs = int(os.getenv("GPU_AGENT_LOCAL_SESSION_TIMEOUT_SECS", "30"))


# ==== Logging ====
logger = logging.getLogger("GPU-Agent")
logger.setLevel(logging.INFO)

console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

file = logging.FileHandler(AGENT_LOG, encoding="utf-8")
file.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logger.addHandler(console)
logger.addHandler(file)


def now():
    return pytz.timezone(TIMEZONE).localize(dt.datetime.now())


# ==== nvidia-smi wrapper ====


def nsmi_query_compute():
    """
    Get a list of GPU usage info.

    Returns:
        list(dict): [{"pid": pid, "uuid": gpu_uuid, "used_mem": used_vram}]
    """
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,gpu_uuid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception as e:
        logger.error(f"Failed to check GPU process. Error message: {e}")
        return []

    res = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            logger.warning(f"Unexpected value from GPU compute app: {line}")
            continue
        try:
            pid = int(parts[0])
            uuid = parts[1]
            mem = int(parts[2])
        except ValueError:
            logger.error(
                f"Failed to retrieve info from GPU compute app info. ValueError: {line}"
            )
            continue
        res.append({"pid": pid, "uuid": uuid, "used_mem": mem})
    return res


def nsmi_query_gpus():
    """
    Get GPU static info for /register.

    Returns:
        list[dict] = [{index, uuid, name, total_memory_mb}, ...]
    """
    gpus = []

    # 1) UUID + name via `nvidia-smi -L`
    try:
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True)
    except Exception as e:
        logger.error(f"Failed to query GPU list: {e}")
        return gpus

    for line in out.strip().splitlines():
        # Example: GPU 0: NVIDIA RTX A6000 (UUID: GPU-xxxx...)
        m = re.match(r"GPU\s+(\d+):\s+(.*?)\s+\(UUID:\s*([^)]+)\)", line)
        if not m:
            logger.warning(f"Unexpected nvidia-smi -L line: {line}")
            continue
        index = int(m.group(1))
        name = m.group(2).strip()
        uuid = m.group(3).strip()
        gpus.append(
            {
                "index": index,
                "uuid": uuid,
                "name": name,
                "total_memory_mb": None,  # fill later
            }
        )

    # 2) total memory via `nvidia-smi --query-gpu=memory.total`
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
        mem_list = [int(x.strip()) for x in out.strip().splitlines() if x.strip()]
    except Exception as e:
        logger.warning(f"Failed to query GPU total memory: {e}")
        mem_list = []

    if mem_list and len(mem_list) == len(gpus):
        for i, mem in enumerate(mem_list):
            gpus[i]["total_memory_mb"] = mem

    return gpus


def pid_username(pid: int) -> str | None:
    """
    Get a username from /proc/<pid>

    Returns:
        str: username
    """
    try:
        st = os.stat(f"/proc/{pid}")
        return pwd.getpwuid(st.st_uid).pw_name
    except FileNotFoundError:
        logger.info(
            f"pid_username: Process file not found, maybe process ended during query. pid: {pid}"
        )
    except Exception as e:
        logger.error(f"Failed to get username with pid. Error message: {e}")
        return None


# ==== Report to ARIS ====


def post(path: str, payload: dict):
    url = GATEWAY_URL.rstrip("/") + BASE_PATH + path
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}"} if AGENT_TOKEN else {}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=3)
    except Exception as e:
        logger.error(f"[GPU-AGENT] POST {path} exception: {e}")
        return None

    try:
        data = r.json()
    except Exception:
        logger.error(
            f"[GPU-AGENT] POST {path} got non-JSON response: {r.status_code} {r.text[:200]}"
        )
        return None

    if not r.ok or (isinstance(data, dict) and not data.get("ok", True)):
        logger.error(
            f"[GPU-AGENT] POST {path} failed: status={r.status_code}, body={data}"
        )
        return None

    return data


# ==== Local session tracking ====


class LocalSession:
    def __init__(self, gpu_uuid, user, started_at):
        self.gpu_uuid = gpu_uuid
        self.user = user
        self.started_at = started_at
        self.last_seen = started_at

    @property
    def key(self):
        return (self.gpu_uuid, self.user)


def main_loop():
    # Active session (gpu_uuid, user) -> LocalSession
    sessions: dict[tuple[str, str], LocalSession] = {}

    registered_ok = False
    last_register_ts = None
    register_interval_secs = 60  # retry every one minute

    while True:
        ts = now()

        if (not registered_ok) and (
            last_register_ts is None
            or (ts - last_register_ts).total_seconds() > register_interval_secs
        ):
            # Report to ARIS the set of GPUs installed
            gpus = nsmi_query_gpus()
            register_payload = {
                "hostname": HOSTNAME,
                "agent_version": AGENT_VERSION,
                "gpus": gpus,
                "ts": ts.isoformat(),
            }
            resp = post("/register", register_payload)
            if resp is not None:
                registered_ok = True
                last_register_ts = ts
                logger.info("[GPU-AGENT] register success")
            else:
                registered_ok = False
                last_register_ts = ts
                logger.warning("[GPU-AGENT] register failed, will retry later")
                time.sleep(register_interval_secs)

        compute = nsmi_query_compute()

        # Construct snapshot for this iteration: uuid -> { user -> set(pids) }
        by_uuid_users: dict[str, dict[str, set[int]]] = {}
        for record in compute:
            user = pid_username(record["pid"])
            if not user:
                continue
            d = by_uuid_users.setdefault(record["uuid"], {})
            d.setdefault(user, set()).add(record["pid"])

        # Actual key set that is currently active
        current_keys = set()

        # Gather all heartbeat items
        heartbeat_items = []

        for uuid, user_pids in by_uuid_users.items():
            for user, pids in user_pids.items():
                key = (uuid, user)
                current_keys.add(key)

                if key not in sessions:
                    # found new (gpu, user) session -> tell ARIS start RUNNING
                    sess = LocalSession(uuid, user, ts)
                    sessions[key] = sess

                    post(
                        "/session/start",
                        {
                            "hostname": HOSTNAME,
                            "gpu_uuid": uuid,
                            "user": user,
                            "started_at": ts.isoformat(),
                            "pids": sorted(list(pids)),
                        },
                    )
                else:
                    # existing session, refreash heartbeat
                    sess = sessions[key]
                    sess.last_seen = ts

                    heartbeat_items.append(
                        {
                            "gpu_uuid": uuid,
                            "user": user,
                            "ts": ts.isoformat(),
                            "pids": sorted(list(pids)),
                        },
                    )

        # Send one heartbeat request for all sessions
        if heartbeat_items:
            post(
                "/session/heartbeat",
                {
                    "hostname": HOSTNAME,
                    "items": heartbeat_items,
                },
            )

        # Find sessions that no longer exist, check for ending process
        to_delete = []
        for key, sess in sessions.items():
            if key not in current_keys:
                # if already disappeared, directly end the session
                # Use a very short TIMEOUT for robust
                if (ts - sess.last_seen).total_seconds() >= session_timeout_secs:
                    post(
                        "/session/end",
                        {
                            "hostname": HOSTNAME,
                            "gpu_uuid": sess.gpu_uuid,
                            "user": sess.user,
                            "started_at": sess.started_at.isoformat(),
                            "ended_at": sess.last_seen.isoformat(),
                        },
                    )
                    to_delete.append(key)

        for key in to_delete:
            sessions.pop(key, None)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main_loop()
