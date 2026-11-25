from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.models import (
    AgentArchiveState,
    AgentMessage,
    AgentMessageReceipt,
    ArchivedComputeTime,
    GpuDevice,
    Node,
    NodeResource,
)
from aris.modules.vega.reports import AgentReport
from aris.modules.vega.storage_schemas import MessageStorage, ObservedWindowStorage
from aris.modules.vega.target_schemas import GpuInventoryLabel
from aris.modules.vega.usage_archive_schemas import MAX_ARCHIVE_MESSAGES, UsageArchiveFreeze


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


@dataclass(frozen=True)
class MessageAcknowledgment:
    fingerprint: str
    applied_to_current: bool


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
        found = self.session.execute(
            select(Node, AgentArchiveState.retired_through_seq)
            .outerjoin(
                AgentArchiveState,
                and_(
                    AgentArchiveState.agent_id == Node.agent_id,
                    AgentArchiveState.node_id == Node.id,
                ),
            )
            .where(Node.id == batch.node_id)
            .with_for_update(of=Node)
        ).one_or_none()
        if found is None:
            return None
        node: Node = found[0]
        retired = found[1]
        if retired is None and node.agent_id == batch.agent_id:
            self.session.add(AgentArchiveState(agent_id=node.agent_id, node_id=node.id))
            self.session.flush()
        return node

    def protect_archive_source(self, agent_id: str, node_id: str) -> int:
        row = AgentArchiveState
        retired = self.session.scalar(
            select(row.retired_through_seq)
            .where(
                row.agent_id == agent_id,
                row.node_id == node_id,
            )
            .with_for_update(read=True)
        )
        if retired is None:
            # Explicitly registered but still empty nodes need their first guard.
            node = self.session.execute(
                select(Node.last_seq)
                .where(
                    Node.id == node_id,
                    Node.agent_id == agent_id,
                )
                .with_for_update()
            ).one_or_none()
            if node is None or node.last_seq != 0:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Archive source boundary is missing", 503
                )
            dialect = self.session.get_bind().dialect.name
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            self.session.execute(
                insert(row)
                .values(agent_id=agent_id, node_id=node_id)
                .on_conflict_do_nothing(index_elements=[row.agent_id])
            )
            retired = self.session.scalar(
                select(row.retired_through_seq)
                .where(
                    row.agent_id == agent_id,
                    row.node_id == node_id,
                )
                .with_for_update(read=True)
            )
            if retired is None:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Archive source boundary changed", 503
                )
        return retired

    def lock_archive_retirement(self, agent_id: str, node_id: str) -> int:
        row = AgentArchiveState
        retired = self.session.scalar(
            select(row.retired_through_seq)
            .where(
                row.agent_id == agent_id,
                row.node_id == node_id,
            )
            .with_for_update()
        )
        if retired is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Archive source boundary is missing", 503)
        return retired

    def message(self, agent_id: str, message_id: str) -> MessageAcknowledgment | None:
        archived = AgentMessage
        receipt = AgentMessageReceipt
        rows = self.session.execute(
            select(archived.seq, archived.fingerprint, archived.applied_to_current)
            .where(archived.agent_id == agent_id, archived.message_id == message_id)
            .union(
                select(receipt.seq, receipt.fingerprint, receipt.applied_to_current).where(
                    receipt.agent_id == agent_id, receipt.message_id == message_id
                )
            )
        ).all()
        if len(rows) > 1:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Stored message receipts disagree", 503)
        return MessageAcknowledgment(rows[0][1], rows[0][2]) if rows else None

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

    def usage_source_node(self, agent_id: str) -> str | None:
        return self.session.scalar(select(Node.id).where(Node.agent_id == agent_id))

    def usage_source_ceiling(self, agent_id: str) -> tuple[str, int, int | None] | None:
        archived = (
            select(func.max(AgentMessage.seq))
            .where(AgentMessage.agent_id == Node.agent_id)
            .correlate(Node)
            .scalar_subquery()
        )
        row = self.session.execute(
            select(Node.id, Node.last_seq, archived).where(Node.agent_id == agent_id)
        ).one_or_none()
        return (row[0], row[1], row[2]) if row is not None else None

    def usage_range_metadata(
        self, agent_id: str, after_seq: int, through_seq: int
    ) -> tuple[tuple[str, int, int], ...]:
        rows = self.session.execute(
            select(
                AgentMessage.message_id,
                AgentMessage.seq,
                func.length(cast(AgentMessage.payload, String)),
            )
            .where(
                AgentMessage.agent_id == agent_id,
                AgentMessage.seq > after_seq,
                AgentMessage.seq <= through_seq,
            )
            .order_by(AgentMessage.seq)
            .limit(MAX_ARCHIVE_MESSAGES)
        )
        return tuple((row[0], row[1], row[2]) for row in rows)

    def usage_messages(
        self, agent_id: str, message_ids: tuple[str, ...]
    ) -> tuple[StoredUsageMessage, ...]:
        if len(message_ids) > MAX_ARCHIVE_MESSAGES:
            raise ValueError("original message lookup exceeds its bounded ID set")
        if not message_ids:
            return ()
        # The unique (agent_id, message_id) key bounds the result. SQL ordering
        # plus a parameterized LIMIT can make a prepared generic plan scan the
        # agent's unrelated history; sort this small verified set in memory.
        rows = self.session.execute(
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
            ).where(AgentMessage.agent_id == agent_id, AgentMessage.message_id.in_(message_ids))
        )
        return tuple(sorted((StoredUsageMessage(*row) for row in rows), key=lambda row: row.seq))

    def usage_message_metadata(
        self, agent_id: str, message_ids: tuple[str, ...]
    ) -> tuple[tuple[str, int, int], ...]:
        if len(message_ids) > MAX_ARCHIVE_MESSAGES:
            raise ValueError("original metadata lookup exceeds its bounded ID set")
        if not message_ids:
            return ()
        rows = self.session.execute(
            select(
                AgentMessage.message_id,
                AgentMessage.seq,
                func.length(cast(AgentMessage.payload, String)),
            ).where(AgentMessage.agent_id == agent_id, AgentMessage.message_id.in_(message_ids))
        )
        return tuple(sorted(((row[0], row[1], row[2]) for row in rows), key=lambda row: row[1]))

    def usage_spans_metadata(
        self, agent_id: str, spans: tuple[tuple[int, int], ...]
    ) -> tuple[tuple[str, int, int], ...]:
        if not 1 <= len(spans) <= 64 or any(not 1 <= a <= b < a + 128 for a, b in spans):
            raise ValueError("source spans exceed their bounded sequence set")
        rows = self.session.execute(
            select(
                AgentMessage.message_id,
                AgentMessage.seq,
                func.length(cast(AgentMessage.payload, String)),
            ).where(
                AgentMessage.agent_id == agent_id,
                or_(*(AgentMessage.seq.between(a, b) for a, b in spans)),
            )
        )
        return tuple(sorted(((r[0], r[1], r[2]) for r in rows), key=lambda r: r[1]))

    def sequence_exists(self, agent_id: str, seq: int) -> bool:
        archived = AgentMessage
        receipt = AgentMessageReceipt
        present = self.session.scalar(
            select(archived.seq)
            .where(archived.agent_id == agent_id, archived.seq == seq)
            .union_all(select(receipt.seq).where(receipt.agent_id == agent_id, receipt.seq == seq))
            .limit(1)
        )
        return present is not None

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

    def observed_window_storage(
        self, agent_id: str, node_id: str, start: datetime, end: datetime
    ) -> ObservedWindowStorage:
        row = AgentMessage
        count, late, first, last = self.session.execute(
            select(
                func.count(),
                func.count().filter(row.received_at >= end),
                func.min(row.seq),
                func.max(row.seq),
            ).where(
                row.agent_id == agent_id,
                row.node_id == node_id,
                row.observed_at >= start,
                row.observed_at < end,
            )
        ).one()
        return ObservedWindowStorage(
            messages=count, late_arrivals=late, first_seq=first, last_seq=last
        )

    def oldest_payload_metadata(
        self, agent_id: str, limit: int
    ) -> tuple[tuple[int, str, datetime, datetime], ...]:
        row = AgentMessage
        rows = self.session.execute(
            select(row.seq, row.node_id, row.observed_at, row.received_at)
            .where(row.agent_id == agent_id)
            .order_by(row.seq)
            .limit(limit)
        )
        return tuple(
            (seq, node, utc(observed), utc(received)) for seq, node, observed, received in rows
        )

    def retired_sequence_count_before(self, agent_id: str, seq: int) -> int:
        receipt = AgentMessageReceipt
        return self.session.execute(
            select(func.count())
            .select_from(receipt)
            .where(receipt.agent_id == agent_id, receipt.seq > 0, receipt.seq < seq)
        ).scalar_one()

    def arrivals_outside_freezes(self, freezes: tuple[UsageArchiveFreeze, ...]) -> tuple[int, ...]:
        if not freezes:
            return ()
        row = AgentMessage
        compute = ArchivedComputeTime
        counts = union_all(
            *(
                select(literal(index).label("day_ordinal"), func.count().label("arrivals"))
                .select_from(row)
                .outerjoin(compute, compute.message_row_id == row.id)
                .where(
                    row.agent_id == freeze.source.agent_id,
                    row.node_id == freeze.source.node_id,
                    row.observed_at >= freeze.start_at,
                    or_(row.received_at > freeze.read_at, row.seq > freeze.through_seq),
                    or_(
                        row.observed_at < freeze.end_at,
                        and_(
                            row.message_kind == "telemetry",
                            or_(
                                compute.observed_at.is_(None),
                                and_(
                                    compute.observed_at >= freeze.start_at,
                                    compute.observed_at < freeze.end_at,
                                ),
                            ),
                        ),
                    ),
                )
                for index, freeze in enumerate(freezes)
            )
        )
        observed = {index: arrivals for index, arrivals in self.session.execute(counts)}
        return tuple(observed[index] for index in range(len(freezes)))

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
