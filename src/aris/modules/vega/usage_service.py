"""One immutable source message per lookup; no current-state inference or external I/O."""

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_schemas import (
    GpuUsageFact,
    LinuxGpuOwner,
    UsageObservationFact,
    UsageObservationQuery,
)


def _usage_fact(
    batch: ResourceTelemetryBatch, *, fingerprint: str, received_at: datetime
) -> UsageObservationFact:
    """Project validated source data only; current node/GPU tables are never consulted."""
    resource = batch.resources
    compute = resource.compute
    inventory = {gpu.gpu_uuid: gpu for gpu in resource.gpu.gpus}
    driver = {gpu.gpu_uuid: gpu for gpu in compute.gpus}
    total = compute.reported_processes
    counts_consistent = (
        compute.quality == "ok"
        and total is not None
        and (
            total <= sum(item.reported_processes for item in driver.values())
            and (total != 0 or not any(item.reported_processes for item in driver.values()))
        )
    )
    ownership = compute.ownership
    result = []
    for gpu_uuid in sorted(set(inventory) | set(driver)):
        label = inventory.get(gpu_uuid)
        count = (
            (driver[gpu_uuid].reported_processes if gpu_uuid in driver else 0)
            if counts_consistent
            else None
        )
        owners = []
        if count is not None and ownership is not None:
            for user in ownership.users:
                part = next((item for item in user.gpus if item.gpu_uuid == gpu_uuid), None)
                if part is not None:
                    owners.append(
                        LinuxGpuOwner(
                            uid=user.uid,
                            username=user.username,
                            reported_processes=part.reported_processes,
                        )
                    )
        owners.sort(key=lambda owner: owner.uid)
        unattributed = (
            count - sum(owner.reported_processes for owner in owners) if count is not None else None
        )
        quality: Literal["complete", "partial", "unknown"] = (
            "unknown"
            if count is None
            else (
                "complete"
                if unattributed == 0 and all(owner.username is not None for owner in owners)
                else "partial"
            )
        )
        result.append(
            GpuUsageFact(
                gpu_uuid=gpu_uuid,
                inventory_present=label is not None,
                index=label.index if label else None,
                model=label.model if label else None,
                reported_processes=count,
                owners=tuple(owners),
                unattributed_processes=unattributed,
                ownership_quality=quality,
            )
        )
    return UsageObservationFact(
        agent_id=batch.agent_id,
        node_id=batch.node_id,
        message_id=batch.message_id,
        seq=batch.seq,
        source_fingerprint=fingerprint,
        observed_at=batch.observed_at,
        received_at=received_at,
        gpu_observed_at=resource.gpu.observed_at,
        compute_observed_at=compute.observed_at,
        gpu_quality=resource.gpu.quality,
        compute_quality=compute.quality,
        compute_counts_consistent=counts_consistent,
        gpus=tuple(result),
    )


class VegaUsageObservations:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observation(self, query: UsageObservationQuery) -> UsageObservationFact | None:
        with self.sessions() as session:
            return self.observation_in_transaction(session, query)

    def observation_in_transaction(
        self, session: Session, query: UsageObservationQuery
    ) -> UsageObservationFact | None:
        with session.no_autoflush:
            row = VegaRepository(session).usage_message(query.agent_id, str(query.message_id))
            if row is None:
                return None
            if row.node_id != query.node_id or row.seq != query.expected_seq:
                raise DomainError("CONFLICT", "Observation source binding differs", 409)
            if row.message_kind != "telemetry":
                return None
            try:
                fingerprint = hashlib.sha256(
                    json.dumps(row.payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if fingerprint != row.fingerprint:
                    raise ValueError("stored observation fingerprint differs")
                batch = ResourceTelemetryBatch.model_validate(row.payload)
                if (
                    batch.agent_id,
                    batch.node_id,
                    str(batch.message_id),
                    batch.seq,
                    batch.observed_at,
                ) != (row.agent_id, row.node_id, row.message_id, row.seq, utc(row.observed_at)):
                    raise ValueError("stored observation identity or time differs")
                return _usage_fact(batch, fingerprint=fingerprint, received_at=utc(row.received_at))
            except (ValueError, TypeError, ValidationError):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Historical usage observation is invalid", 503
                ) from None
