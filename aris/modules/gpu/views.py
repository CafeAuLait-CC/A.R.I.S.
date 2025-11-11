import datetime as dt
from sqlalchemy.orm import Session
from ...core.config import settings
from ...core.logging import get_logger
from . import models, repo as gpu_repo

logger = get_logger(__name__)


def _choose_user_label(u: models.User) -> str:
    # Present the most user-friendly identifier.
    return u.slack_display_name or u.name or u.gpu_node_username or u.cwl or "Unknown"


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


# ===== Slack Home Tab =====


def get_realtime_cluster_view(db: Session) -> dict:
    """
    Return real-time GPU occupation view for Slack Home Tab (Only use RUNNING + fresh heartbeat session)
    Do not count for history (UsageLog), only check current user --> GPU
    """
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=settings.STALE_HEARTBEAT_SECS)

    # All active nodes
    nodes = gpu_repo.get_all_active_nodes(db)

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
