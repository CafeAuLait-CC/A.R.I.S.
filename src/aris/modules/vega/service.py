import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.vega.models import GpuDevice, TelemetryMessage
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.schemas import (
    ClusterStatus,
    GpuStatus,
    IngestResult,
    NodeStatus,
    TelemetryBatch,
)


def utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata; PostgreSQL preserves it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class VegaService:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    def ingest(self, batch: TelemetryBatch, request_id: str) -> IngestResult:
        now = datetime.now(UTC)
        if batch.sent_at > now + timedelta(seconds=self.settings.max_clock_skew_seconds):
            raise DomainError("VALIDATION_ERROR", "Agent clock is too far in the future", 422)
        payload = batch.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            repo = VegaRepository(session)
            node = repo.lock_node(batch, now)
            if node is None or node.agent_id != batch.agent_id:
                raise DomainError(
                    "PERMISSION_DENIED", "Node and agent identity binding differs", 403
                )
            existing = repo.message(batch.agent_id, str(batch.message_id))
            if existing:
                if existing.fingerprint != fingerprint:
                    raise DomainError("CONFLICT", "Message ID reused with different content", 409)
                return IngestResult(
                    message_id=batch.message_id,
                    duplicate=True,
                    applied_to_current=existing.applied_to_current,
                )
            if repo.sequence_exists(batch.agent_id, batch.seq):
                raise DomainError("CONFLICT", "Sequence reused by a different message", 409)
            applied = batch.seq > node.last_seq and batch.observed_at >= utc(node.observed_at)
            previous_seq = node.last_seq
            node.last_seq = max(node.last_seq, batch.seq)
            if applied:
                node.hostname = batch.hostname
                node.agent_version = batch.agent_version
                node.observed_at = batch.observed_at
                node.received_at = now
                for previous_gpu in repo.gpus(batch.node_id):
                    previous_gpu.present = False
                for sample in batch.gpus:
                    gpu = session.get(GpuDevice, sample.gpu_uuid)
                    if gpu is not None and gpu.node_id != batch.node_id:
                        raise DomainError("CONFLICT", "GPU UUID belongs to another node", 409)
                    if gpu is None:
                        gpu = GpuDevice(gpu_uuid=sample.gpu_uuid, node_id=batch.node_id)
                        session.add(gpu)
                    for field, value in sample.model_dump().items():
                        setattr(gpu, field, value)
                    gpu.present = True
            session.add(
                TelemetryMessage(
                    id=str(uuid4()),
                    message_id=str(batch.message_id),
                    agent_id=batch.agent_id,
                    node_id=batch.node_id,
                    seq=batch.seq,
                    fingerprint=fingerprint,
                    observed_at=batch.observed_at,
                    received_at=now,
                    applied_to_current=applied,
                    payload=payload,
                )
            )
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="vega.telemetry.accepted",
                    event_version=1,
                    aggregate_type="node",
                    aggregate_id=batch.node_id,
                    aggregate_version=batch.seq,
                    occurred_at=batch.observed_at,
                    recorded_at=now,
                    actor=batch.agent_id,
                    correlation_id=request_id,
                    causation_id=str(batch.message_id),
                    payload={"message_id": str(batch.message_id), "applied_to_current": applied},
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=batch.agent_id,
                    action="vega.telemetry.accepted",
                    target=batch.node_id,
                    request_id=request_id,
                    recorded_at=now,
                    changes={
                        "previous_seq": previous_seq,
                        "received_seq": batch.seq,
                        "applied_to_current": applied,
                    },
                )
            )
        return IngestResult(
            message_id=batch.message_id, duplicate=False, applied_to_current=applied
        )

    def status(self) -> ClusterStatus:
        now = datetime.now(UTC)
        result: list[NodeStatus] = []
        with self.sessions() as session:
            repo = VegaRepository(session)
            for node in repo.nodes():
                age = (now - utc(node.observed_at)).total_seconds()
                health: Literal["ONLINE", "STALE", "OFFLINE"] = "ONLINE"
                if age >= self.settings.offline_after_seconds:
                    health = "OFFLINE"
                elif age >= self.settings.stale_after_seconds:
                    health = "STALE"
                gpus: list[GpuStatus] = []
                for gpu in repo.gpus(node.id):
                    observed: Literal["idle", "busy", "unknown"] = "unknown"
                    if health == "ONLINE" and gpu.present:
                        observed = (
                            "busy" if gpu.memory_used_mib or gpu.utilization_percent else "idle"
                        )
                    gpus.append(
                        GpuStatus(
                            gpu_uuid=gpu.gpu_uuid,
                            index=gpu.index,
                            model=gpu.model,
                            memory_total_mib=gpu.memory_total_mib,
                            memory_used_mib=gpu.memory_used_mib,
                            utilization_percent=gpu.utilization_percent,
                            temperature_c=gpu.temperature_c,
                            present=gpu.present,
                            observed_state=observed,
                        )
                    )
                result.append(
                    NodeStatus(
                        node_id=node.id,
                        hostname=node.hostname,
                        agent_id=node.agent_id,
                        agent_version=node.agent_version,
                        status=health,
                        observed_at=utc(node.observed_at),
                        received_at=utc(node.received_at),
                        seq=node.last_seq,
                        gpus=gpus,
                    )
                )
        return ClusterStatus(generated_at=now, nodes=result)
