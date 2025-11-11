from sqlalchemy.orm import Session
from . import models

# ==== User ====


def get_user_by_node_username(db: Session, username: str) -> models.User | None:
    return (
        db.query(models.User)
        .filter(
            models.User.gpu_node_username == username,
            models.User.active.is_(True),
        )
        .one_or_none()
    )


def create_user_by_node_username(db: Session, username: str) -> models.User:
    user = models.User(
        name="Unknown User",
        gpu_node_username=username,
    )
    db.add(user)
    db.flush()
    return user


# ==== Node / GPU ====


def get_or_create_node(db: Session, hostname: str) -> models.GpuNode:
    node = db.query(models.GpuNode).filter_by(hostname=hostname).one_or_none()
    if not node:
        node = models.GpuNode(hostname=hostname)
        db.add(node)
        db.flush()
    return node


def get_all_active_nodes(db: Session) -> list[models.GpuNode]:
    return (
        db.query(models.GpuNode)
        .filter(models.GpuNode.active.is_(True))
        .order_by(models.GpuNode.hostname.asc())
        .all()
    )


def get_gpu_by_uuid(db: Session, uuid: str) -> models.Gpu | None:
    return db.query(models.Gpu).filter_by(uuid=uuid).one_or_none()


def get_or_create_gpu(
    db: Session,
    node: models.GpuNode,
    info,
) -> models.Gpu:
    gpu = get_gpu_by_uuid(db, info.uuid)
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


# ==== Session / UsageLog ====


def get_reserved_session_for_start(db: Session, user_id: str, gpu_id: str, start_at):
    # Check for reserved session (simple matching. Should improve matching method)
    # TODO: Upgrade matching method. reserved_start < payload.start_at < reserved_until. Already added the line, should verify!
    return (
        db.query(models.GpuSession)
        .filter(
            models.GpuSession.user_id == user_id,
            models.GpuSession.gpu_id == gpu_id,
            models.GpuSession.state == models.SessionState.RESERVED,
            models.GpuSession.reserved_until >= start_at,
            models.GpuSession.reserved_from <= start_at,
            # added this condition, but may cause problem:
            # what if start before reserved_from and end after it?
        )
        .order_by(models.GpuSession.reserved_from.asc())
        .first()
    )
