# aris/modules/gpu/service.py

from sqlalchemy.orm import Session
import datetime as dt

from . import models, schemas
from ...core.config import settings
from ...core import events
from ...core.notify import notify


def _choose_user_label(u: models.User) -> str:
    # Present the most user-friendly identifier.
    return u.slack_display_name or u.cwl or u.gpu_node_username or u.name or "Unknown"


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


class GpuService:
    """GPU Related Services：
    - internal /internal/gpu/* for agents
    - Slack /slack/* Frontend can use the query methods (future features)
    """

    # ===== Basic Tools =====

    def ensure_user_for_gpu_username(self, db: Session, username: str) -> models.User:
        user = (
            db.query(models.User)
            .filter(
                models.User.gpu_node_username == username,
                models.User.active.is_(True),
            )
            .one_or_none()
        )
        if user:
            return user

        # new user: create Unknown User first, wait for admin to complete registration for this user.
        user = models.User(
            name="Unknown User",
            gpu_node_username=username,
        )
        db.add(user)
        db.flush()

        # TODO: Notify admin to complete user registration via Slack messages.
        # notify.notify_channel("ADMIN_CHANNEL_ID", f"New GPU user detected: {username} (id={user.id})")
        events.event_bus.publish(
            "user.new_from_gpu",
            {"user_id": user.id, "gpu_node_username": username},
        )
        return user

    def _get_or_create_node(self, db: Session, hostname: str) -> models.GpuNode:
        node = db.query(models.GpuNode).filter_by(hostname=hostname).one_or_none()
        if not node:
            node = models.GpuNode(hostname=hostname)
            db.add(node)
            db.flush()
        return node

    def _get_or_create_gpu(
        self,
        db: Session,
        node: models.GpuNode,
        info: schemas.AgentGpuInfo,
    ) -> models.Gpu:
        gpu = db.query(models.Gpu).filter_by(uuid=info.uuid).one_or_none()
        if not gpu:
            gpu = models.Gpu(
                node_id=node.id,
                index=info.index,
                uuid=info.uuid,
                name=info.name,
                total_memory_mb=info.total_memory_mb,
            )
            db.add(gpu)
            db.flush()
        else:
            gpu.node_id = node.id
            gpu.index = info.index
            if info.name:
                gpu.name = info.name
            if info.total_memory_mb:
                gpu.total_memory_mb = info.total_memory_mb
        return gpu

    # ===== /internal/gpu/register =====

    def handle_register(
        self, db: Session, payload: schemas.AgentRegisterRequest
    ) -> dict:
        node = self._get_or_create_node(db, payload.hostname)
        node.agent_version = payload.agent_version

        gpu_mapping: dict[str, str] = {}
        for g in payload.gpus:
            gpu = self._get_or_create_gpu(db, node, g)
            gpu_mapping[g.uuid] = gpu.id

        db.commit()
        return {"ok": True, "node_id": node.id, "gpu_mapping": gpu_mapping}

    # ===== /internal/gpu/session/start =====

    def handle_session_start(
        self, db: Session, payload: schemas.AgentSessionStartRequest
    ) -> schemas.AgentSessionResponse:
        node = self._get_or_create_node(db, payload.hostname)
        gpu = db.query(models.Gpu).filter_by(uuid=payload.gpu_uuid).one_or_none()
        if not gpu:
            # In case agent did not register this gpu
            gpu_info = schemas.AgentGpuInfo(uuid=payload.gpu_uuid, index=0)
            gpu = self._get_or_create_gpu(db, node, gpu_info)

            # TODO: log.warning(f"gpu {payload.gpu_uuid} auto-created during session start (agent not registered?)")

        user = self.ensure_user_for_gpu_username(db, payload.user)

        # Check for reserved session (simple matching. Should improve matching method)
        # TODO: Upgrade matching method. reserved_start < payload.start_at < reserved_until. Already added the line, should verify!
        sess = (
            db.query(models.GpuSession)
            .filter(
                models.GpuSession.user_id == user.id,
                models.GpuSession.gpu_id == gpu.id,
                models.GpuSession.state == models.SessionState.RESERVED,
                models.GpuSession.reserved_until >= payload.started_at,
                models.GpuSession.reserved_from
                <= payload.started_at,  # added this condition, but may cause problem:
                # what if start before reserved_from and end after it?
            )
            .order_by(models.GpuSession.reserved_from.asc())
            .first()
        )

        if sess:
            # Write reservation time range into UsageLog
            if sess.reserved_from and sess.reserved_from < payload.started_at:
                minutes = int(
                    max(
                        0,
                        (payload.started_at - sess.reserved_from).total_seconds() // 60,
                    )
                )
                if minutes > 0:
                    db.add(
                        models.UsageLog(
                            user_id=user.id,
                            gpu_id=gpu.id,
                            node_id=node.id,
                            session_id=sess.id,
                            start_ts=sess.reserved_from,
                            end_ts=payload.started_at,
                            minutes=minutes,
                            tag=models.UsageTag.RESERVED,  # TODO: maybe replace it with "WARM UP"?
                        )
                    )

            sess.state = models.SessionState.RUNNING
            sess.started_at = payload.started_at
            sess.heartbeat_at = payload.started_at
        else:
            # No reservation, create RUNNING session directly
            sess = models.GpuSession(
                user_id=user.id,
                gpu_id=gpu.id,
                node_id=node.id,
                state=models.SessionState.RUNNING,
                started_at=payload.started_at,
                heartbeat_at=payload.started_at,
                created_by="agent",
            )
            db.add(sess)

        db.commit()
        return schemas.AgentSessionResponse(ok=True, session_id=sess.id)

    # ===== /internal/gpu/session/heartbeat =====

    def handle_session_heartbeat(
        self, db: Session, payload: schemas.AgentSessionHeartbeatRequest
    ) -> schemas.AgentSessionResponse:
        node = self._get_or_create_node(db, payload.hostname)
        gpu = db.query(models.Gpu).filter_by(uuid=payload.gpu_uuid).one_or_none()
        if not gpu:
            return schemas.AgentSessionResponse(ok=False, detail="unknown gpu")

        user = self.ensure_user_for_gpu_username(db, payload.user)

        sess = (
            db.query(models.GpuSession)
            .filter(
                models.GpuSession.user_id == user.id,
                models.GpuSession.gpu_id == gpu.id,
                models.GpuSession.state == models.SessionState.RUNNING,
            )
            .order_by(models.GpuSession.started_at.desc())
            .first()
        )

        if not sess:
            # started session lost, create a new one
            sess = models.GpuSession(
                user_id=user.id,
                gpu_id=gpu.id,
                node_id=node.id,
                state=models.SessionState.RUNNING,
                started_at=payload.ts,
                heartbeat_at=payload.ts,
                created_by="agent",
                note="auto-created by heartbeat",
            )
            db.add(sess)
        else:
            sess.heartbeat_at = payload.ts

        db.commit()
        return schemas.AgentSessionResponse(ok=True, session_id=sess.id)

    # ===== /internal/gpu/session/end =====

    def handle_session_end(
        self, db: Session, payload: schemas.AgentSessionEndRequest
    ) -> schemas.AgentSessionResponse:
        node = self._get_or_create_node(db, payload.hostname)
        gpu = db.query(models.Gpu).filter_by(uuid=payload.gpu_uuid).one_or_none()
        if not gpu:
            return schemas.AgentSessionResponse(ok=False, detail="unknown gpu")

        user = self.ensure_user_for_gpu_username(db, payload.user)

        sess = (
            db.query(models.GpuSession)
            .filter(
                models.GpuSession.user_id == user.id,
                models.GpuSession.gpu_id == gpu.id,
                models.GpuSession.state == models.SessionState.RUNNING,
            )
            .order_by(models.GpuSession.started_at.desc())
            .first()
        )

        if not sess:
            # session lost, silent success, return details
            return schemas.AgentSessionResponse(
                ok=False, detail="no active session to end"
            )

        ended_at = payload.ended_at
        sess.state = models.SessionState.ENDED
        sess.ended_at = ended_at
        sess.heartbeat_at = ended_at

        if sess.started_at:
            total_secs = (ended_at - sess.started_at).total_seconds()
            minutes = max(1, int(total_secs // 60))
            db.add(
                models.UsageLog(
                    user_id=user.id,
                    gpu_id=gpu.id,
                    node_id=node.id,
                    session_id=sess.id,
                    start_ts=sess.started_at,
                    end_ts=ended_at,
                    minutes=minutes,
                    tag=models.UsageTag.NORMAL,
                )
            )

        db.commit()
        return schemas.AgentSessionResponse(ok=True, session_id=sess.id)

    # ===== Slack / GPU Placeholder (Future Features) =====

    def handle_slash(self, db: Session, user: models.User, text: str) -> str:
        # TODO: implement /gpu status, /gpu my, /reserve, etc.
        return "ARIS GPU module is online. Commands to be implemented."

    # ===== Slack Home Tab =====

    def get_realtime_cluster_view(self, db: Session) -> dict:
        """
        Return real-time GPU occupation view for Slack Home Tab (Only use RUNNING + fresh heartbeat session)
        Do not count for history (UsageLog), only check current user --> GPU
        """
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(seconds=settings.STALE_HEARTBEAT_SECS)

        # All active nodes
        nodes = (
            db.query(models.GpuNode)
            .filter(models.GpuNode.active.is_(True))
            .order_by(models.GpuNode.hostname.asc())
            .all()
        )

        # Query for all RUNNING sessions + user + GPU
        sessions = (
            db.query(models.GpuSession)
            .join(models.Gpu)
            .join(models.User)
            .filter(
                models.GpuSession.state == models.SessionState.RUNNING,
                models.GpuSession.heartbeat_at.isnot(None),
            )
            .all()
        )

        # Group by gpu_id
        sessions_by_gpu: dict[str, list[models.GpuSession]] = {}
        for s in sessions:
            sessions_by_gpu.setdefault(s.gpu_id, []).append(s)

        total_gpus = 0
        active_gpus = 0

        node_views: list[dict] = []

        for node in nodes:
            gpus = sorted(node.gpus, key=lambda g: g.index)
            if not gpus:
                continue

            gpu_views: list[dict] = []

            for gpu in gpus:
                total_gpus += 1
                sess_list = sessions_by_gpu.get(gpu.id, []) or []

                # Only session with fresh heartbeat
                fresh = [
                    s for s in sess_list if s.heartbeat_at and s.heartbeat_at >= cutoff
                ]

                if fresh:
                    active_gpus += 1
                    # If multiple session on the same GPU, display all users
                    users = ", ".join(_choose_user_label(s.user) for s in fresh)
                    # Use most recent started_at as running time reference
                    started_ats = [s.started_at for s in fresh if s.started_at]
                    if started_ats:
                        started_min = min(started_ats)
                        elapsed = int((now - started_min).total_seconds())
                        since_text = _fmt_duration(elapsed)
                    else:
                        since_text = "running"

                    state = "in_use"
                    summary = f"{users} · {since_text}"
                else:
                    state = "idle"
                    summary = "Idle"

                gpu_views.append(
                    {
                        "index": gpu.index,
                        "name": gpu.name or gpu.uuid,
                        "memory_mb": gpu.total_memory_mb,
                        "state": state,  # "in_use" / "idle"
                        "summary": summary,  # One sentence summary
                    }
                )

            node_views.append(
                {
                    "hostname": node.hostname,
                    "gpus": gpu_views,
                }
            )

        idle_gpus = max(0, total_gpus - active_gpus)

        return {
            "updated_at": now,
            "total_gpus": total_gpus,
            "active_gpus": active_gpus,
            "idle_gpus": idle_gpus,
            "nodes": node_views,
        }


gpu_service = GpuService()
