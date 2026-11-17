from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.time import utc
from aris.modules.vega.models import AgentMessage, GpuDevice, Node, NodeResource
from aris.modules.vega.reports import AgentReport
from aris.modules.vega.storage_schemas import MessageStorage
from aris.modules.vega.target_schemas import GpuInventoryLabel


@dataclass(frozen=True)
class GpuTargetRecord:
    gpu_uuid: str
    node_id: str
    present: bool
    last_seen_at: datetime
    last_received_at: datetime
    observed_at: datetime | None
    received_at: datetime | None
    telemetry_seq: int
    collector_status: Literal["ok", "error", "unknown"]
    collector_health_at: datetime | None


@dataclass(frozen=True)
class StoredUsageMessage:
    agent_id: str
    node_id: str
    message_id: str
    seq: int
    message_kind: str
    observed_at: datetime
    received_at: datetime
    fingerprint: str
    payload: dict[str, Any]


class VegaRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def gpu_label(self, gpu_uuid: str) -> GpuInventoryLabel | None:
        row = self.session.execute(
            select(GpuDevice.gpu_uuid, GpuDevice.node_id, GpuDevice.index, GpuDevice.model).where(
                GpuDevice.gpu_uuid == gpu_uuid
            )
        ).one_or_none()
        return (
            GpuInventoryLabel(gpu_uuid=row[0], node_id=row[1], index=row[2], model=row[3])
            if row
            else None
        )

    def gpu_labels(self, gpu_uuids: tuple[str, ...]) -> tuple[GpuInventoryLabel, ...]:
        rows = self.session.execute(
            select(GpuDevice.gpu_uuid, GpuDevice.node_id, GpuDevice.index, GpuDevice.model)
            .where(GpuDevice.gpu_uuid.in_(gpu_uuids))
            .order_by(GpuDevice.gpu_uuid)
        )
        return tuple(
            GpuInventoryLabel(gpu_uuid=row[0], node_id=row[1], index=row[2], model=row[3])
            for row in rows
        )

    def gpu_catalog(self, limit: int) -> tuple[GpuInventoryLabel, ...]:
        rows = self.session.execute(
            select(GpuDevice.gpu_uuid, GpuDevice.node_id, GpuDevice.index, GpuDevice.model)
            .where(GpuDevice.present.is_(True))
            .order_by(GpuDevice.node_id, GpuDevice.index, GpuDevice.gpu_uuid)
            .limit(limit + 1)
        )
        return tuple(
            GpuInventoryLabel(gpu_uuid=row[0], node_id=row[1], index=row[2], model=row[3])
            for row in rows
        )

    def lock_node(self, batch: AgentReport, now: datetime) -> Node | None:
        dialect = self.session.get_bind().dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        statement = (
            insert(Node)
            .values(
                id=batch.node_id,
                agent_id=batch.agent_id,
                hostname=batch.hostname,
                agent_version=batch.agent_version,
                last_seq=0,
                observed_at=utc(batch.observed_at),
                received_at=now,
            )
            # Both identities are unique; concurrent first reports may hit either index.
            .on_conflict_do_nothing()
        )
        self.session.execute(statement)
        return self.session.scalars(
            select(Node).where(Node.id == batch.node_id).with_for_update()
        ).one_or_none()

    def message(self, agent_id: str, message_id: str) -> AgentMessage | None:
        return self.session.scalar(
            select(AgentMessage).where(
                AgentMessage.agent_id == agent_id, AgentMessage.message_id == message_id
            )
        )

    def usage_message(self, agent_id: str, message_id: str) -> StoredUsageMessage | None:
        row = self.session.execute(
            select(
                AgentMessage.agent_id,
                AgentMessage.node_id,
                AgentMessage.message_id,
                AgentMessage.seq,
                AgentMessage.message_kind,
                AgentMessage.observed_at,
                AgentMessage.received_at,
                AgentMessage.fingerprint,
                AgentMessage.payload,
            ).where(AgentMessage.agent_id == agent_id, AgentMessage.message_id == message_id)
        ).one_or_none()
        return StoredUsageMessage(*row) if row is not None else None

    def sequence_exists(self, agent_id: str, seq: int) -> bool:
        return (
            self.session.scalar(
                select(AgentMessage.id).where(
                    AgentMessage.agent_id == agent_id, AgentMessage.seq == seq
                )
            )
            is not None
        )

    def nodes(self) -> Sequence[Node]:
        return self.session.scalars(select(Node).order_by(Node.id)).all()

    def gpus(self, node_id: str) -> Sequence[GpuDevice]:
        return self.session.scalars(
            select(GpuDevice)
            .where(GpuDevice.node_id == node_id)
            .order_by(GpuDevice.index, GpuDevice.gpu_uuid)
        ).all()

    def gpu(self, gpu_uuid: str) -> GpuDevice | None:
        return self.session.get(GpuDevice, gpu_uuid)

    def node_exists(self, node_id: str) -> bool:
        return self.session.scalar(select(Node.id).where(Node.id == node_id)) is not None

    def gpu_target(self, gpu_uuid: str) -> GpuTargetRecord | None:
        # Scalar columns bypass possibly stale ORM identities in a caller's
        # transaction; one statement sees node/GPU from one database snapshot.
        row = self.session.execute(
            select(
                GpuDevice.gpu_uuid,
                GpuDevice.node_id,
                GpuDevice.present,
                Node.observed_at,
                Node.received_at,
                Node.telemetry_at,
                Node.telemetry_received_at,
                Node.telemetry_seq,
                Node.collector_status,
                Node.collector_health_at,
            )
            .join(Node, Node.id == GpuDevice.node_id)
            .where(GpuDevice.gpu_uuid == gpu_uuid)
        ).one_or_none()
        return GpuTargetRecord(*row) if row is not None else None

    def resource(self, node_id: str) -> NodeResource | None:
        return self.session.get(NodeResource, node_id)

    def message_storage(self, cutoff: datetime | None) -> list[MessageStorage]:
        before = (
            func.sum(case((AgentMessage.received_at < cutoff, 1), else_=0))
            if cutoff
            else func.count()
        )
        rows = self.session.execute(
            select(
                AgentMessage.message_kind,
                func.count(),
                func.min(AgentMessage.received_at),
                func.max(AgentMessage.received_at),
                before,
            )
            .group_by(AgentMessage.message_kind)
            .order_by(AgentMessage.message_kind)
        )
        return [
            MessageStorage(
                message_kind=row[0],
                count=row[1],
                first_received_at=utc(row[2]),
                last_received_at=utc(row[3]),
                before_cutoff=row[4] if cutoff else None,
            )
            for row in rows
        ]
