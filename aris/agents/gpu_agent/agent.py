import datetime as dt
import logging
import os
import pwd
import re
import subprocess
import time

import pytz
import requests

# ==== Basic Configuration ====
GATEWAY_URL = os.getenv("ARIS_GATEWAY_URL", "http://hostname")
BASE_PATH = os.getenv("GPU_AGENT_BASE_PATH", "/gpu")
AGENT_TOKEN = os.getenv("GPU_AGENT_TOKEN", "")
POLL_SECS = int(os.getenv("GPU_AGENT_POLL_SECS", "10"))
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
HOSTNAME = os.getenv("GPU_AGENT_HOSTNAME") or os.uname().nodename
AGENT_LOG = os.getenv("GPU_AGENT_LOG", "./GPU_AGENT_LOG.log")

session_timeout_secs = int(os.getenv("GPU_AGENT_LOCAL_SESSION_TIMEOUT_SECS", "30"))


# ==== Set Logging Service ====
logger = logigng.getLogger("GPU-Agent")
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
        logger.error(f'Failed to check GPU process. Error message: {e}')
        return []

    res = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            logger.warning(f'Unexpected value from GPU compute app: {line}')
            continue
        try:
            pid = int(parts[0])
            uuid = parts[1]
            mem = int(parts[2])
        except ValueError:
            logger.error(f'Failed to retrieve info from GPU compute app info. ValueError: {line}')
            continue
        res.append({"pid": pid, "uuid": uuid, "used_mem": mem})
    return res


def nsmi_query_uuids():
    """
    Get a list of GPU UUIDs. 

    Returns:
        list(str): ["xxxx-xxxx-xxxx-xxxx"]
    """
    try:
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True)
    except Exception as e:
        logger.error(f'Falied to retrieve GPU UUID. Error message: {e}')
        return []

    uuids = []
    for line in out.strip().splitlines():
        m = re.search(r"UUID:\s*([A-Za-z0-9\-]+)", line)
        if m:
            uuids.append(m.group(1))
    return uuids


def pid_username(pid: int) -> str | None:
    """
    Get a username in linux system, given the pid 

    Returns:
        str: username
    """
    try:
        st = os.stat(f"/proc/{pid}")
        return pwd.getpwuid(st.st_uid).pw_name
    except Exception as e:
        logger.error(f'Failed to get username with pid. Error message: {e}')
        return None

# ==== Report to ARIS ====

def post(path: str, payload: dict):
    url = GATEWAY_URL.rstrip("/") + BASE_PATH + path
    headers = {"Authorization": f"Bearer {AGENT_TOKEN}"} if AGENT_TOKEN else {}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=3)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"[GPU-AGENT] POST {path} failed: {e}")

# ==== GPU Session Tracking (local memory only, used to detect start/end) ====

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

    # Report to ARIS the set of GPUs installed
    uuids = nsmi_query_uuids()
    post("/register", {
        "host": HOSTNAME,
        "gpu_uuids": uuids,
        "ts": now().isoformat(),
    })

    while True:
        ts = now()
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
        for uuid, user_pids in by_uuid_users.items():
            for user, pids in user_pids.items():
                key = (uuid, user)
                current_keys.add(key)

                if key not in sessions:
                    # found new (gpu, user) session -> tell ARIS start RUNNING
                    sess = LocalSession(uuid, user, ts)
                    sessions[key] = sess

                    post("/session/start", {
                        "host": HOSTNAME,
                        "gpu_uuid": uuid,
                        "user": user,
                        "started_at": ts.isoformat(),
                        "pids": sorted(list(pids)),
                    })
                else:
                    # existing session, refreash heartbeat
                    sess = sessions[key]
                    sess.last_seen = ts

                    post("/session/heartbeat", {
                        "host": HOSTNAME,
                        "gpu_uuid": uuid,
                        "user": user,
                        "ts": ts.isoformat(),
                        "pids": sorted(list(pids)),
                    })

        # Find sessions that no longer exist, check for ending process
        to_delete = []
        for key, sess in sessions.items():
            if key not in current_keys:
                # if already disappeared, directly end the session
                # Use a very short TIMEOUT for robust
                if (ts - sess.last_seen).total_seconds() >= session_timeout_secs:
                    post("/session/end", {
                        "host": HOSTNAME,
                        "gpu_uuid": sess.gpu_uuid,
                        "user": sess.user,
                        "started_at": sess.started_at.isoformat(),
                        "ended_at": sess.last_seen.isoformat(),
                    })
                    to_delete.append(key)

        for key in to_delete:
            sessions.pop(key, None)

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main_loop()
