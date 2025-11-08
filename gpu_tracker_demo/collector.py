import os, time, subprocess, pwd, re, datetime as dt, pytz
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from models import User, GPU, GPUSession, UsageLog, SessionState
import config as cfg

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gpupool.sqlite3")
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
engine = create_engine(DATABASE_URL, future=True)

def now():
    return pytz.timezone(TIMEZONE).localize(dt.datetime.now())

def nsmi_query_compute():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return []
    res = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3: continue
        try:
            pid = int(parts[0]); uuid = parts[1]; mem = int(parts[2])
        except ValueError:
            continue
        res.append({"pid": pid, "uuid": uuid, "used_mem": mem})
    return res

def nsmi_query_uuids():
    try:
        out = subprocess.check_output(["nvidia-smi", "-L"], text=True)
        return [re.search(r"UUID:\s*([A-Za-z0-9\-]+)", line).group(1)
                for line in out.strip().splitlines() if "UUID:" in line]
    except Exception:
        return []

def map_uuid_to_name(session):
    uuids = nsmi_query_uuids()
    return {uuid: f"gpu{i}" for i, uuid in enumerate(uuids)}

def pid_username(pid):
    try:
        st = os.stat(f"/proc/{pid}")
        return pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        return None

def find_or_create_user(session, name):
    u = session.scalars(select(User).where(User.name==name)).first()
    if not u:
        u = User(name=name, weekly_quota_minutes=100*60, active=True)
        session.add(u); session.commit()
    return u

def log_reserved_slice(session, sess, end_ts):
    until = sess.reserved_at + dt.timedelta(minutes=sess.reserve_minutes)
    reserved_end = end_ts if end_ts < until else until
    elapsed = int((reserved_end - sess.reserved_at).total_seconds() // 60)
    if elapsed > 0:
        session.add(UsageLog(user_id=sess.user_id, gpu_id=sess.gpu_id,
                             start_ts=sess.reserved_at, end_ts=reserved_end,
                             minutes=elapsed, tag="reserved"))

def end_running_now(session, sess, end_ts):
    elapsed = int((end_ts - sess.started_at).total_seconds() // 60)
    if elapsed > 0:
        session.add(UsageLog(user_id=sess.user_id, gpu_id=sess.gpu_id,
                             start_ts=sess.started_at, end_ts=end_ts,
                             minutes=elapsed, tag="running"))
    sess.state = SessionState.ENDED
    sess.ended_at = end_ts

def main_loop(poll_secs=10):
    with Session(engine) as s:
        uuid_to_name = map_uuid_to_name(s)

    while True:
        try:
            ts = now()
            compute = nsmi_query_compute()

            # uuid -> { user: set(pids) }
            by_uuid_users = {}
            for rec in compute:
                user = pid_username(rec["pid"]) ; 
                if not user: 
                    continue
                d = by_uuid_users.setdefault(rec["uuid"], {})
                d.setdefault(user, set()).add(rec["pid"])

            with Session(engine) as s:
                # refresh uuid map opportunistically
                new_map = map_uuid_to_name(s)
                if new_map: uuid_to_name = new_map

                current_active_keys = set()  # {(gpu_id, user_id)}
                for uuid, user_pids in by_uuid_users.items():
                    gpu_name = uuid_to_name.get(uuid)
                    if not gpu_name: continue
                    gpu_row = s.scalars(select(GPU).where(GPU.name==gpu_name)).first()
                    if not gpu_row: continue

                    for username, pids in user_pids.items():
                        u = find_or_create_user(s, username)
                        key = (gpu_row.id, u.id)
                        current_active_keys.add(key)

                        # Find active session for this (gpu,user)
                        sess = s.scalars(select(GPUSession)
                                         .where(GPUSession.gpu_id==gpu_row.id,
                                                GPUSession.user_id==u.id,
                                                GPUSession.state!=SessionState.ENDED)
                                         .order_by(GPUSession.id.desc())).first()

                        if not sess:
                            # Non-reserve path: start RUNNING immediately at first detection
                            s.add(GPUSession(user_id=u.id, gpu_id=gpu_row.id, state=SessionState.RUNNING,
                                             reserved_at=None, reserve_minutes=0,
                                             started_at=ts, heartbeat_at=ts, note="detected"))
                        else:
                            if sess.state == SessionState.RESERVED:
                                # Flip to RUNNING and settle reserved actual minutes
                                log_reserved_slice(s, sess, ts)
                                sess.state = SessionState.RUNNING
                                sess.started_at = ts
                                sess.heartbeat_at = ts
                            elif sess.state == SessionState.RUNNING:
                                # Keep alive
                                sess.heartbeat_at = ts

                # End RUNNING sessions that have no processes in this snapshot (last PID disappeared)
                for sess in s.scalars(select(GPUSession).where(GPUSession.state==SessionState.RUNNING)).all():
                    key = (sess.gpu_id, sess.user_id)
                    if key not in current_active_keys:
                        end_running_now(s, sess, ts)

                s.commit()

        except Exception:
            pass

        time.sleep(poll_secs)

if __name__ == "__main__":
    main_loop()
