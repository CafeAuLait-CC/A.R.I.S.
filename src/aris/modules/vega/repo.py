from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.modules.vega.models import GpuDevice, Node, TelemetryMessage
from aris.modules.vega.schemas import TelemetryBatch


class VegaRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_node(self, batch: TelemetryBatch, now: datetime) -> Node | None:
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
                observed_at=batch.observed_at,
                received_at=now,
            )
            # Both identities are unique; concurrent first reports may hit either index.
            .on_conflict_do_nothing()
        )
        self.session.execute(statement)
        return self.session.scalars(
            select(Node).where(Node.id == batch.node_id).with_for_update()
        ).one_or_none()

    def message(self, agent_id: str, message_id: str) -> TelemetryMessage | None:
        return self.session.scalar(
            select(TelemetryMessage).where(
                TelemetryMessage.agent_id == agent_id, TelemetryMessage.message_id == message_id
            )
        )

    def sequence_exists(self, agent_id: str, seq: int) -> bool:
        return (
            self.session.scalar(
                select(TelemetryMessage.id).where(
                    TelemetryMessage.agent_id == agent_id, TelemetryMessage.seq == seq
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
