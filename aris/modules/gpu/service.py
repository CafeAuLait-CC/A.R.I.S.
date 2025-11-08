from sqlalchemy.orm import Session
from datetime import datetime
from . import models, schemas
from ...core import events
from ...core.notify import notify


class GpuService:
    def handle_slash(self, db: Session, user: models.User, text: str) -> str:
        """
        /gpu command (simplified):
        - empty or 'status' -> return current status brief
        - 'my' -> return my record
        simple commands for now, expend to 'reserve' later.
        """
        args = (text or "").strip().split()

        if len(args) == 0 or args[0] == "status":
            return self._status_summary(db)

        if args[0] == "my":
            return self._user_usage(db, user)

        return "Usage: `/gpu` status，`/gpu my` list your log。"

    def _status_summary(self, db: Session) -> str:
        latest = (
            db.query(models.GpuStatus)
            .order_by(models.GpuStatus.timestamp.desc())
            .limit(50)
            .all()
        )
        if not latest:
            return "There is NO GPU status to report. Please check the agent configuration for each node. "

        lines = []
        for s in latest:
            lines.append(
                f"{s.node.name} [GPU{s.gpu_index}] "
                f"{s.memory_used_mb}MB / procs:{s.process_count} / user:{s.username or '-'}"
            )
        return "Recent GPU status:\n" + "\n".join(lines)

    def _user_usage(self, db: Session, user: models.User) -> str:
        # simply chech reservation, could be expended as needed
        qs = (
            db.query(models.GpuReservation)
            .filter(models.GpuReservation.user_id == user.id)
            .order_by(models.GpuReservation.start_time.desc())
            .limit(10)
            .all()
        )
        if not qs:
            return "There is no GPU reservation/log related to you."

        lines = []
        for r in qs:
            lines.append(
                f"{r.gpu} {r.status} "
                f"{r.start_time} -> {r.end_time or '...'}"
            )
        return "Your recent log: \n" + "\n".join(lines)

    def handle_report(self, db: Session, report: schemas.GpuReport):
        node = (
            db.query(models.GpuNode)
            .filter(models.GpuNode.name == report.node_name,
                    models.GpuNode.token == report.token)
            .one_or_none()
        )
        if not node:
            # decline if node not registered
            return False

        node.last_report = report.timestamp

        for p in report.processes:
            s = models.GpuStatus(
                node_id=node.id,
                gpu_index=p.gpu_index,
                username=p.username,
                process_count=p.process_count,
                memory_used_mb=p.memory_used_mb,
                timestamp=report.timestamp,
            )
            db.add(s)

        db.commit()

        # publish internal event (used to trigger warnings, etc.)
        events.event_bus.publish("gpu.usage.updated", report.dict())
        return True


gpu_service = GpuService()
