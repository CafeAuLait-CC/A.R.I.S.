"""Read bounded original GPU samples by exact archive references, without current state."""

from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.gpu_hardware_schemas import (
    GpuHardwareBatch,
    GpuHardwareBatchQuery,
    GpuHardwareObservation,
    hardware_batch_fingerprint,
)
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference
from aris.modules.vega.usage_service import _original_report


class VegaGpuHardwareObservations:
    def __init__(self, sessions: sessionmaker[Session], *, timeout_ms: int = 3000) -> None:
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 10000:
            raise ValueError("hardware source read timeout must be bounded")
        self.sessions, self.timeout_ms = sessions, timeout_ms

    @staticmethod
    def _database_failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "GPU hardware archive database is unavailable",
            503,
        )

    def batch(self, query: GpuHardwareBatchQuery) -> GpuHardwareBatch:
        try:
            with self.sessions() as session:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(
                    session, min(100, self.timeout_ms), min(1000, self.timeout_ms), self.timeout_ms
                )
                return self.batch_in_transaction(session, query)
        except DBAPIError as error:
            raise self._database_failure(error) from None

    def batch_in_transaction(
        self, session: Session, query: GpuHardwareBatchQuery
    ) -> GpuHardwareBatch:
        query = GpuHardwareBatchQuery.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with session.no_autoflush:
                repo = VegaRepository(session)
                agent = query.references[0].agent_id
                node = query.references[0].node_id
                if repo.usage_source_node(agent) != node:
                    raise DomainError("CONFLICT", "Hardware archive source binding differs", 409)
                keys = tuple(str(item.message_id) for item in query.references)
                metadata = repo.usage_message_metadata(agent, keys)
                if len(metadata) != len(keys):
                    raise DomainError("NOT_FOUND", "Original hardware source is absent", 404)
                if tuple((row[0], row[1]) for row in metadata) != tuple(
                    (str(item.message_id), item.seq) for item in query.references
                ):
                    raise DomainError("CONFLICT", "Hardware archive references differ", 409)
                characters = sum(row[2] for row in metadata)
                if characters > query.source_character_budget:
                    raise DomainError(
                        "USAGE_SOURCE_TOO_LARGE", "Hardware archive exceeds its source budget", 503
                    )
                rows = repo.usage_messages(agent, keys)
                if len(rows) != len(keys):
                    raise ValueError("original hardware source changed during read")
                observations = []
                for row, reference in zip(rows, query.references, strict=True):
                    original = _original_report(row, agent, node)
                    actual = ArchivedUsageReference(
                        agent_id=row.agent_id,
                        node_id=row.node_id,
                        message_id=row.message_id,
                        seq=row.seq,
                        kind=row.message_kind,
                        fingerprint=row.fingerprint,
                        observed_at=utc(row.observed_at),
                        received_at=utc(row.received_at),
                    )
                    if actual != reference:
                        raise DomainError("CONFLICT", "Hardware archive references differ", 409)
                    if not isinstance(original, ResourceTelemetryBatch):
                        raise ValueError("hardware source must be original resource telemetry")
                    component = original.resources.gpu
                    observations.append(
                        GpuHardwareObservation(
                            reference=actual,
                            gpu_observed_at=component.observed_at,
                            gpu_quality=component.quality,
                            gpus=tuple(component.gpus),
                        )
                    )
                    if monotonic() >= deadline:
                        raise DomainError(
                            "RETRYABLE_DATABASE_ERROR", "Hardware archive read budget exceeded", 503
                        )
                draft = GpuHardwareBatch.model_construct(
                    query=query,
                    observations=tuple(observations),
                    source_characters=characters,
                    fingerprint="0" * 64,
                )
                return GpuHardwareBatch.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": hardware_batch_fingerprint(draft),
                    }
                )
        except DBAPIError as error:
            raise self._database_failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Original GPU hardware archive is inconsistent", 503
            ) from None
