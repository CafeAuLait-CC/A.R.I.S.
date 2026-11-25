import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.disk_schemas import DISK_STALE_AFTER_SECONDS
from aris.modules.vega.gpu_activity import gpu_activity
from aris.modules.vega.models import (
    AgentMessage,
    ArchivedComputeTime,
    GpuDevice,
    Node,
    NodeResource,
)
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.reports import AgentReport
from aris.modules.vega.resource_schemas import (
    NodeResourceStatus,
    ResourceObservation,
    ResourceStatus,
    ResourceTelemetryBatch,
)
from aris.modules.vega.schemas import (
    ClusterStatus,
    GpuSample,
    GpuStatus,
    Heartbeat,
    IngestResult,
    NodeStatus,
)
from aris.modules.vega.storage_schemas import ObservationStorage


class VegaService:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings

    def storage(self, cutoff: datetime | None = None) -> ObservationStorage:
        if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
            raise ValueError("retention preview requires a time zone")
        cutoff = utc(cutoff) if cutoff is not None else None
        with self.sessions() as session:
            messages = VegaRepository(session).message_storage(cutoff)
        return ObservationStorage(
            generated_at=datetime.now(UTC),
            cutoff=cutoff,
            messages=messages,
            blockers=[
                "Original message identities and acknowledgments provide durable deduplication",
                "Domain events, pending Outbox, audit and future ledger need separate retention",
                "Payload exit requires verified consumer, report and health dependencies",
            ],
        )

    def ingest(self, batch: AgentReport, request_id: str) -> IngestResult:
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
            if batch.seq > node.last_seq and batch.observed_at >= utc(node.observed_at):
                node.hostname = batch.hostname
                node.agent_version = batch.agent_version
                node.observed_at = utc(batch.observed_at)
                node.received_at = now
            node.last_seq = max(node.last_seq, batch.seq)
            if isinstance(batch, Heartbeat):
                kind = "heartbeat"
                applied = batch.seq > node.heartbeat_seq and (
                    node.heartbeat_at is None or batch.observed_at >= utc(node.heartbeat_at)
                )
                node.heartbeat_seq = max(node.heartbeat_seq, batch.seq)
                if applied:
                    node.heartbeat_at = utc(batch.observed_at)
                    node.heartbeat_received_at = now
                    self._collector_health(
                        node, batch.seq, batch.observed_at, batch.collector_status
                    )
            else:
                kind = "telemetry"
                applied = self._apply_resources(session, repo, node, batch, now)
            session.add(
                AgentMessage(
                    id=str(uuid4()),
                    message_id=str(batch.message_id),
                    agent_id=batch.agent_id,
                    node_id=batch.node_id,
                    seq=batch.seq,
                    message_kind=kind,
                    fingerprint=fingerprint,
                    observed_at=utc(batch.observed_at),
                    compute_time=(
                        ArchivedComputeTime(observed_at=batch.resources.compute.observed_at)
                        if isinstance(batch, ResourceTelemetryBatch)
                        else None
                    ),
                    received_at=now,
                    applied_to_current=applied,
                    payload=payload,
                )
            )
            # The archived fact and its original acknowledgment are the durable
            # transport record. Successful polling is not a business event:
            # Lyra consumes the archive, and no relay subscribes to packet ACKs.
        return IngestResult(
            message_id=batch.message_id, duplicate=False, applied_to_current=applied
        )

    @staticmethod
    def _apply_gpus(
        session: Session, repo: VegaRepository, node_id: str, samples: list[GpuSample]
    ) -> None:
        for previous_gpu in repo.gpus(node_id):
            previous_gpu.present = False
        for sample in samples:
            gpu = repo.gpu(sample.gpu_uuid)
            if gpu is not None and gpu.node_id != node_id:
                raise DomainError("CONFLICT", "GPU UUID belongs to another node", 409)
            if gpu is None:
                gpu = GpuDevice(gpu_uuid=sample.gpu_uuid, node_id=node_id)
                session.add(gpu)
            for field, value in sample.model_dump().items():
                setattr(gpu, field, value)
            gpu.present = True

    @staticmethod
    def _collector_health(
        node: Node, seq: int, observed_at: datetime, status: Literal["ok", "error"]
    ) -> None:
        applied = seq > node.collector_health_seq and (
            node.collector_health_at is None or observed_at >= utc(node.collector_health_at)
        )
        node.collector_health_seq = max(seq, node.collector_health_seq)
        if applied:
            node.collector_health_at = utc(observed_at)
            node.collector_status = status

    def _apply_resources(
        self,
        session: Session,
        repo: VegaRepository,
        node: Node,
        batch: ResourceTelemetryBatch,
        now: datetime,
    ) -> bool:
        observation = batch.resources
        stored = repo.resource(node.id)
        applied = stored is None or (
            batch.seq > stored.last_seq and observation.observed_at >= utc(stored.observed_at)
        )
        if stored is None:
            stored = NodeResource(node_id=node.id, last_seq=batch.seq)
            session.add(stored)
        stored.last_seq = max(stored.last_seq, batch.seq)
        if applied:
            if stored.observation is not None:
                previous = ResourceObservation.model_validate(stored.observation)
                # Cached components retain their own authority. A newer GPU frame
                # cannot make an older CPU or compute observation current again.
                observation = observation.model_copy(
                    update={
                        name: getattr(previous, name)
                        for name in ("gpu", "system", "compute")
                        if getattr(observation, name).observed_at
                        < getattr(previous, name).observed_at
                    }
                )
                if previous.disks is not None and (
                    observation.disks is None
                    or observation.disks.observed_at < previous.disks.observed_at
                ):
                    observation = observation.model_copy(update={"disks": previous.disks})
            stored.seq = batch.seq
            stored.observed_at = observation.observed_at
            stored.received_at = now
            stored.observation = observation.model_dump(mode="json")
        gpu = observation.gpu
        gpu_applied = batch.seq > node.telemetry_seq and (
            node.telemetry_at is None or gpu.observed_at >= utc(node.telemetry_at)
        )
        node.telemetry_seq = max(node.telemetry_seq, batch.seq)
        if gpu_applied and gpu.quality == "ok":
            node.telemetry_at = gpu.observed_at
            node.telemetry_received_at = now
            self._apply_gpus(session, repo, node.id, gpu.gpus)
        self._collector_health(
            node, batch.seq, gpu.observed_at, "ok" if gpu.quality == "ok" else "error"
        )
        return applied or gpu_applied

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
                quality: Literal["CURRENT", "STALE", "MISSING", "ERROR"] = "MISSING"
                if node.telemetry_at is not None:
                    quality = "CURRENT"
                    if (
                        now - utc(node.telemetry_at)
                    ).total_seconds() >= self.settings.stale_after_seconds:
                        quality = "STALE"
                    elif (
                        node.collector_status == "error"
                        and node.collector_health_at is not None
                        and utc(node.collector_health_at) >= utc(node.telemetry_at)
                    ):
                        quality = "ERROR"
                stored = repo.resource(node.id)
                compute = (
                    ResourceObservation.model_validate(stored.observation).compute
                    if stored is not None
                    else None
                )
                processes = (
                    {part.gpu_uuid: part.reported_processes for part in compute.gpus}
                    if compute is not None
                    and compute.quality == "ok"
                    and -self.settings.max_clock_skew_seconds
                    <= (now - compute.observed_at).total_seconds()
                    < self.settings.stale_after_seconds
                    else {}
                )
                gpus: list[GpuStatus] = []
                for gpu in repo.gpus(node.id):
                    observed: Literal["idle", "busy", "unknown"] = "unknown"
                    if health == "ONLINE" and quality == "CURRENT" and gpu.present:
                        observed = gpu_activity(
                            gpu.memory_used_mib,
                            gpu.utilization_percent,
                            processes.get(gpu.gpu_uuid),
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
                        observed_at=utc(node.telemetry_at) if node.telemetry_at else None,
                        received_at=utc(node.telemetry_received_at)
                        if node.telemetry_received_at
                        else None,
                        last_seen_at=utc(node.observed_at),
                        last_received_at=utc(node.received_at),
                        heartbeat_at=utc(node.heartbeat_at) if node.heartbeat_at else None,
                        heartbeat_received_at=utc(node.heartbeat_received_at)
                        if node.heartbeat_received_at
                        else None,
                        collector_status=node.collector_status,
                        telemetry_status=quality,
                        seq=node.last_seq,
                        gpus=gpus,
                    )
                )
        return ClusterStatus(generated_at=now, nodes=result)

    def resource_status(self) -> ResourceStatus:
        base = self.status()
        now = datetime.now(UTC)
        nodes: list[NodeResourceStatus] = []
        with self.sessions() as session:
            repo = VegaRepository(session)
            for node in base.nodes:
                stored = repo.resource(node.node_id)
                if stored is None:
                    nodes.append(
                        NodeResourceStatus(
                            node=node, system_freshness="MISSING", compute_freshness="MISSING"
                        )
                    )
                    continue
                observation = ResourceObservation.model_validate(stored.observation)
                system_current = (
                    node.status == "ONLINE"
                    and (now - observation.system.observed_at).total_seconds()
                    < self.settings.stale_after_seconds
                )
                compute_current = (
                    node.status == "ONLINE"
                    and (now - observation.compute.observed_at).total_seconds()
                    < self.settings.stale_after_seconds
                )
                disks = observation.disks
                disk_current = (
                    disks is not None
                    and node.status == "ONLINE"
                    and -self.settings.max_clock_skew_seconds
                    <= (now - disks.observed_at).total_seconds()
                    < DISK_STALE_AFTER_SECONDS
                )
                nodes.append(
                    NodeResourceStatus(
                        node=node,
                        resource_seq=stored.seq,
                        system_freshness="CURRENT" if system_current else "STALE",
                        system_observed_at=observation.system.observed_at,
                        system=observation.system if system_current else None,
                        compute_freshness="CURRENT" if compute_current else "STALE",
                        compute_observed_at=observation.compute.observed_at,
                        compute=observation.compute if compute_current else None,
                        disks_freshness=(
                            "MISSING" if disks is None else "CURRENT" if disk_current else "STALE"
                        ),
                        disks_observed_at=disks.observed_at if disks else None,
                        disks=disks if disk_current else None,
                    )
                )
        return ResourceStatus(generated_at=now, nodes=nodes)
