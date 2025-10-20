import os, datetime as dt, pytz
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from tabulate import tabulate
from models import UsageLog, User

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gpupool.sqlite3")
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
engine = create_engine(DATABASE_URL, future=True)

def week_bounds(now=None):
    tz = pytz.timezone(TIMEZONE)
    now = now or tz.localize(dt.datetime.now())
    monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    next_monday = monday + dt.timedelta(days=7)
    return monday, next_monday

def generate_weekly_report():
    start, end = week_bounds()
    with Session(engine) as s:
        logs = s.scalars(select(UsageLog).where(UsageLog.start_ts>=start, UsageLog.end_ts<=end)).all()
        totals = {}
        for L in logs:
            name = s.get(User, L.user_id).name
            totals[name] = totals.get(name, 0) + L.minutes
        rows = [(u, f"{m/60:.1f} h") for u, m in sorted(totals.items())]
        print(f"GPU Usage Summary {start.date()} to {(end-dt.timedelta(days=1)).date()}")
        print(tabulate(rows, headers=["User", "Used (hours)"]))

if __name__ == "__main__":
    generate_weekly_report()
