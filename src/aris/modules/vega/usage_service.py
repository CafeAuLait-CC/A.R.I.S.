"""One immutable source message per lookup; no current-state inference or external I/O."""

import hashlib
import json
from datetime import datetime
from time import monotonic
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.repo import StoredUsageMessage, VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat
from aris.modules.vega.usage_archive_schemas import MAX_ARCHIVE_MESSAGES
from aris.modules.vega.usage_schemas import (
    GpuUsageFact,
    LinuxGpuOwner,
    UsageObservationBatchQuery,
    UsageObservationFact,
    UsageObservationQuery,
)

MAX_BATCH_SOURCE_CHARACTERS = 4 * 1024 * 1024
MAX_BATCH_SEQUENCE_SPAN = 8192


def _original_report(
    row: StoredUsageMessage, agent_id: str, node_id: str
) -> ResourceTelemetryBatch | Heartbeat:
    """Validate each original envelope, including intervening heartbeats."""
    fingerprint = hashlib.sha256(
        json.dumps(row.payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report: ResourceTelemetryBatch | Heartbeat
    if row.message_kind == "telemetry":
        report = ResourceTelemetryBatch.model_validate(row.payload)
    elif row.message_kind == "heartbeat":
        report = Heartbeat.model_validate(row.payload)
    else:
        raise ValueError("unsupported archived message kind")
    if (
        fingerprint != row.fingerprint
        or (row.agent_id, row.node_id) != (agent_id, node_id)
        or (report.agent_id, report.node_id, str(report.message_id), report.seq, report.observed_at)
        != (row.agent_id, row.node_id, row.message_id, row.seq, utc(row.observed_at))
    ):
        raise ValueError("stored original identity, fingerprint or time differs")
    return report


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
            return self._observation(row, query)

    @staticmethod
    def _observation(
        row: StoredUsageMessage | None, query: UsageObservationQuery
    ) -> UsageObservationFact | None:
        if row is None:
            return None
        if row.node_id != query.node_id or row.seq != query.expected_seq:
            raise DomainError("CONFLICT", "Observation source binding differs", 409)
        if row.message_kind != "telemetry":
            return None
        try:
            batch = _original_report(row, query.agent_id, query.node_id)
            if not isinstance(batch, ResourceTelemetryBatch):
                raise ValueError("usage source is not telemetry")
            return _usage_fact(batch, fingerprint=row.fingerprint, received_at=utc(row.received_at))
        except (ValueError, TypeError, ValidationError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Historical usage observation is invalid", 503
            ) from None

    def observations(
        self, query: UsageObservationBatchQuery
    ) -> tuple[UsageObservationFact | None, ...]:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 10000)
                return self.observations_in_transaction(session, query)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Historical usage batch database is unavailable",
                503,
            ) from None

    def observations_in_transaction(
        self, session: Session, query: UsageObservationBatchQuery
    ) -> tuple[UsageObservationFact | None, ...]:
        query = UsageObservationBatchQuery.model_validate(query.model_dump(mode="json"))
        if query.contiguous_source_cutoff is not None:
            return self._contiguous_observations(session, query)
        by_id = {str(item.message_id): item for item in query.queries}
        keys = tuple(by_id)
        facts: dict[str, UsageObservationFact | None] = {}
        characters = 0
        with session.no_autoflush:
            repo = VegaRepository(session)
            for offset in range(0, len(keys), MAX_ARCHIVE_MESSAGES):
                selected = keys[offset : offset + MAX_ARCHIVE_MESSAGES]
                metadata = repo.usage_message_metadata(query.queries[0].agent_id, selected)
                characters += sum(row[2] for row in metadata)
                if characters > MAX_BATCH_SOURCE_CHARACTERS:
                    raise DomainError(
                        "USAGE_SOURCE_TOO_LARGE",
                        "Historical source batch exceeds its read budget",
                        503,
                    )
                present = tuple(row[0] for row in metadata)
                rows = repo.usage_messages(query.queries[0].agent_id, present) if present else ()
                for row in rows:
                    facts[row.message_id] = self._observation(row, by_id[row.message_id])
            return tuple(facts.get(str(item.message_id)) for item in query.queries)

    def _contiguous_observations(
        self, session: Session, query: UsageObservationBatchQuery
    ) -> tuple[UsageObservationFact | None, ...]:
        """An evidence set must include every telemetry in its committed source span."""
        cutoff = query.contiguous_source_cutoff
        assert cutoff is not None
        by_id = {str(item.message_id): item for item in query.queries}
        first = min(item.expected_seq for item in query.queries)
        last = max(item.expected_seq for item in query.queries)
        if last - first + 1 > MAX_BATCH_SEQUENCE_SPAN:
            raise DomainError(
                "USAGE_SOURCE_TOO_LARGE", "Usage sequence span exceeds its budget", 503
            )
        deadline = monotonic() + 10
        facts: dict[str, UsageObservationFact] = {}
        characters = 0
        try:
            with session.no_autoflush:
                repo = VegaRepository(session)
                for after in range(first - 1, last, MAX_ARCHIVE_MESSAGES):
                    through = min(after + MAX_ARCHIVE_MESSAGES, last)
                    metadata = repo.usage_range_metadata(query.queries[0].agent_id, after, through)
                    if tuple(row[1] for row in metadata) != tuple(range(after + 1, through + 1)):
                        raise ValueError("usage source span contains an unfilled sequence")
                    characters += sum(row[2] for row in metadata)
                    if characters > MAX_BATCH_SOURCE_CHARACTERS:
                        raise DomainError(
                            "USAGE_SOURCE_TOO_LARGE",
                            "Usage source span exceeds its read budget",
                            503,
                        )
                    selected = tuple(row[0] for row in metadata)
                    rows = repo.usage_messages(query.queries[0].agent_id, selected)
                    if tuple(row.message_id for row in rows) != selected:
                        raise ValueError("usage source span changed while loading")
                    for row in rows:
                        report = _original_report(
                            row, query.queries[0].agent_id, query.queries[0].node_id
                        )
                        if utc(row.observed_at) > cutoff or utc(row.received_at) > cutoff:
                            raise ValueError("usage source span was not available at its cutoff")
                        if isinstance(report, ResourceTelemetryBatch):
                            wanted = by_id.get(row.message_id)
                            if wanted is None or wanted.expected_seq != row.seq:
                                raise ValueError(
                                    "usage evidence omits or misbinds a telemetry source"
                                )
                            facts[row.message_id] = _usage_fact(
                                report,
                                fingerprint=row.fingerprint,
                                received_at=utc(row.received_at),
                            )
                    if monotonic() >= deadline:
                        raise DomainError(
                            "RETRYABLE_DATABASE_ERROR",
                            "Usage source span read budget exceeded",
                            503,
                        )
                if set(facts) != set(by_id):
                    raise ValueError("usage evidence contains an unavailable telemetry source")
                return tuple(facts[str(item.message_id)] for item in query.queries)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Original usage source span is incomplete or invalid", 503
            ) from None
