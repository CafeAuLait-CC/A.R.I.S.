# aris/modules/gpu/service.py

from sqlalchemy.orm import Session

from . import models, schemas, repo as gpu_repo
from ...core import events
from ...core.notify import notify
from ...core.logging import get_logger

logger = get_logger(__name__)


class GpuService:
    """GPU Related Services：
    - internal /internal/gpu/* for agents
    - Slack /slack/* Frontend can use the query methods (future features)
    """

    # ===== Basic Tools =====

    def ensure_user_for_gpu_username(self, db: Session, username: str) -> models.User:
        user = gpu_repo.get_user_by_node_username(db, username)
        if user:
            return user

        # new user: create Unknown User first, wait for admin to complete registration for this user.
        user = gpu_repo.create_user_by_node_username(db, username)

        # TODO: Notify admin to complete user registration via Slack messages.
        # notify.notify_channel("ADMIN_CHANNEL_ID", f"New GPU user detected: {username} (id={user.id})")
        events.event_bus.publish(
            "user.new_from_gpu",
            {"user_id": user.id, "gpu_node_username": username},
        )
        return user

    # ===== /internal/gpu/register =====

    def handle_register(
        self, db: Session, payload: schemas.AgentRegisterRequest
    ) -> dict:
        node = gpu_repo.get_or_create_node(db, payload.hostname)
        node.agent_version = payload.agent_version

        gpu_mapping: dict[str, str] = {}
        for g in payload.gpus:
            gpu = gpu_repo.get_or_create_gpu(db, node, g)
            gpu_mapping[g.uuid] = gpu.id

        db.commit()
        return {"ok": True, "node_id": node.id, "gpu_mapping": gpu_mapping}

    # ===== /internal/gpu/session/start =====

    def handle_session_start(
        self, db: Session, payload: schemas.AgentSessionStartRequest
    ) -> schemas.AgentSessionResponse:
        node = gpu_repo.get_or_create_node(db, payload.hostname)
        gpu = gpu_repo.get_gpu_by_uuid(db, payload.gpu_uuid)
        if not gpu:
            # In case agent did not register this gpu
            gpu_info = schemas.AgentGpuInfo(uuid=payload.gpu_uuid, index=0)
            gpu = gpu_repo.get_or_create_gpu(db, node, gpu_info)

            logger.warning(
                f"gpu {payload.gpu_uuid} auto-created during session start (agent not registered?)"
            )

        user = self.ensure_user_for_gpu_username(db, payload.user)

        sess = gpu_repo.get_reserved_session_for_start(
            db, user.id, gpu.id, payload.started_at
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
        """
        Handle batched heartbeat updates from GPU Agent.
        Each item reprents (gpu_uuid, user) state observed at the current polling.
        """
        node = gpu_repo.get_or_create_node(db, payload.hostname)
        updated_count = 0

        for item in payload.items:
            gpu = gpu_repo.get_gpu_by_uuid(db, item.gpu_uuid)
            if not gpu:
                # return schemas.AgentSessionResponse(ok=False, detail="unknown gpu")
                # TODO: Handle unknown gpu.
                continue

            user = self.ensure_user_for_gpu_username(db, item.user)

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
                    started_at=item.ts,
                    heartbeat_at=item.ts,
                    created_by="agent",
                    note="auto-created by heartbeat",
                )
                db.add(sess)
            else:
                sess.heartbeat_at = item.ts

            updated_count += 1

        db.commit()
        return schemas.AgentSessionResponse(
            ok=True, detail=f"updated {updated_count} sessions"
        )

    # ===== /internal/gpu/session/end =====

    def handle_session_end(
        self, db: Session, payload: schemas.AgentSessionEndRequest
    ) -> schemas.AgentSessionResponse:
        node = gpu_repo.get_or_create_node(db, payload.hostname)
        gpu = gpu_repo.get_gpu_by_uuid(db, payload.gpu_uuid)
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


gpu_service = GpuService()
