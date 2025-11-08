import os, json, datetime as dt, pytz
from flask import Flask, request, send_from_directory, jsonify
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from models import User, GPU, GPUSession, UsageLog, SessionState
import config as cfg

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gpupool.sqlite3")
WEB_BIND = os.getenv("WEB_BIND", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8060"))
TIMEZONE = os.getenv("TIMEZONE", "America/Vancouver")
engine = create_engine(DATABASE_URL, future=True)
app = Flask(__name__, static_folder="static")

def now():
    return pytz.timezone(TIMEZONE).localize(dt.datetime.now())

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/api/status")
def api_status():
    with Session(engine) as s:
        gpu_rows = s.scalars(select(GPU)).all()
        out = []
        for g in gpu_rows:
            sess = s.scalars(select(GPUSession)
                             .where(GPUSession.gpu_id==g.id, GPUSession.state!=SessionState.ENDED)
                             .order_by(GPUSession.id.desc())).first()
            state = "free"; user=None; until=None; note=None
            if sess:
                if sess.state == SessionState.RESERVED:
                    state = "reserved"; user = s.get(User, sess.user_id).name
                    until = (sess.reserved_at + dt.timedelta(minutes=sess.reserve_minutes)).isoformat()
                elif sess.state == SessionState.RUNNING:
                    state = "running"; user = s.get(User, sess.user_id).name
                note = sess.note
            out.append({"gpu": g.name, "state": state, "user": user, "until": until, "note": note})
        return jsonify(out)

@app.post("/api/reserve")
def api_reserve():
    data = request.json or {}
    user = data.get("user"); gpu = data.get("gpu"); minutes = int(data.get("minutes", 30)); note = data.get("note")
    if minutes not in cfg.RESERVATION_MINUTES_ALLOWED: return {"error":"invalid minutes"}, 400
    with Session(engine) as s:
        u = s.scalars(select(User).where(User.name==user)).first()
        g = s.scalars(select(GPU).where(GPU.name==gpu)).first()
        if not u or not g: return {"error":"unknown user or gpu"}, 400
        existing = s.scalars(select(GPUSession)
                             .where(GPUSession.user_id==u.id, GPUSession.gpu_id==g.id, GPUSession.state!=SessionState.ENDED)).first()
        if existing: return {"error":"this user already has an active session on this GPU"}, 400
        sess = GPUSession(user_id=u.id, gpu_id=g.id, state=SessionState.RESERVED, reserved_at=now(),
                          reserve_minutes=minutes, note=note)
        s.add(sess); s.commit(); return {"ok":True, "session_id": sess.id}

@app.get("/api/usage")
def api_usage():
    user = request.args.get("user")
    with Session(engine) as s:
        q = select(UsageLog)
        if user:
            u = s.scalars(select(User).where(User.name==user)).first()
            if not u: return {"error":"unknown user"}, 400
            q = q.where(UsageLog.user_id==u.id)
        logs = s.scalars(q).all()
        out = []
        for L in logs:
            out.append({"user": s.get(User, L.user_id).name, "gpu": s.get(GPU, L.gpu_id).name,
                        "start": L.start_ts.isoformat(), "end": L.end_ts.isoformat(),
                        "minutes": L.minutes, "tag": L.tag})
        return app.response_class(json.dumps(out), mimetype="application/json")

if __name__ == "__main__":
    app.run(host=WEB_BIND, port=WEB_PORT, debug=True)
