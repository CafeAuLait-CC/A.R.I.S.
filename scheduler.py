import os, time, datetime as dt, pytz
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from models import GPUSession, UsageLog, SessionState
import config as cfg

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gpupool.sqlite3")
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
engine = create_engine(DATABASE_URL, future=True)

def now():
    return pytz.timezone(TIMEZONE).localize(dt.datetime.now())

def sweep_once():
    with Session(engine) as s:
        for sess in s.scalars(select(GPUSession).where(GPUSession.state==SessionState.RESERVED)).all():
            until = sess.reserved_at + dt.timedelta(minutes=sess.reserve_minutes)
            if now() > until:
                elapsed = int((until - sess.reserved_at).total_seconds() // 60)
                if elapsed > 0:
                    s.add(UsageLog(user_id=sess.user_id, gpu_id=sess.gpu_id,
                                   start_ts=sess.reserved_at, end_ts=until,
                                   minutes=elapsed, tag="reserved"))
                sess.state = SessionState.ENDED; sess.ended_at = until

        for sess in s.scalars(select(GPUSession).where(GPUSession.state==SessionState.RUNNING)).all():
            if sess.heartbeat_at and (now() - sess.heartbeat_at).total_seconds() > cfg.STALE_SECS:
                end = sess.heartbeat_at
                elapsed = int((end - sess.started_at).total_seconds() // 60)
                if elapsed > 0:
                    s.add(UsageLog(user_id=sess.user_id, gpu_id=sess.gpu_id,
                                   start_ts=sess.started_at, end_ts=end,
                                   minutes=elapsed, tag="running"))
                sess.state = SessionState.ENDED; sess.ended_at = end
        s.commit()

if __name__ == "__main__":
    while True:
        sweep_once(); time.sleep(60)
