"""Verify a bounded contiguous source prefix once; never delete or measure source data."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.source_prefix_repo import SourcePrefixRepository
from aris.modules.lyra.source_prefix_schemas import (
    AdvanceSourcePrefix,
    StoredSourcePrefix,
    source_prefix_fingerprint,
)
from aris.modules.lyra.trace_attestation_repo import TraceAttestationRepository
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
)


class SourcePrefixService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        proofs: TraceAttestationService,
        *,
        configuration: UsageTraceConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 30000:
            raise ValueError("source prefix capture requires a bounded timeout")
        self.sessions, self.proofs, self.configuration = sessions, proofs, configuration
        self.clock, self.timeout_ms = clock or (lambda: datetime.now(UTC)), timeout_ms

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("source prefix clock requires timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(
            "RETRYABLE_DATABASE_ERROR"
            if getattr(error.orig, "sqlstate", None) == "23505"
            else database_failure(error),
            "Source prefix database is unavailable",
            503,
        )

    def exact(self, id: UUID) -> StoredSourcePrefix:
        try:
            with self.sessions() as db:
                transaction_budget(db, 100, 1000, self.timeout_ms)
                value = SourcePrefixRepository(db).get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Source prefix checkpoint not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def advance(
        self, context: UsageTraceConsumeContext, request: AdvanceSourcePrefix
    ) -> StoredSourcePrefix:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Source prefix capture is paused", 503)
        config = UsageTraceConfiguration.model_validate(self.configuration.model_dump(mode="json"))
        request = AdvanceSourcePrefix.model_validate(request.model_dump(mode="json"))
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with self.sessions() as db:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                elif db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN"))
                transaction_budget(db, 100, 1000, self.timeout_ms)
                UsageTraceRepository(db).lock("source_prefix", str(request.id))
                repo = SourcePrefixRepository(db)
                existing = repo.get(request.id)
                if existing is not None:
                    if existing.request != request or existing.configuration != config:
                        raise DomainError(
                            "CONFLICT", "Source prefix operation has different input", 409
                        )
                    return existing
                if request.expected_configuration_fingerprint != config.fingerprint:
                    raise DomainError("CONFLICT", "Source prefix configuration differs", 409)
                previous = None
                if request.previous is not None:
                    previous = repo.get(request.previous.id)
                    if (
                        previous is None
                        or previous.reference() != request.previous
                        or previous.configuration != config
                    ):
                        raise DomainError("CONFLICT", "Previous source prefix differs", 409)
                revision = previous.proofs[-1].revision if previous else 0
                position = UsageTraceRepository(db).position(config)
                if position.revision <= revision:
                    raise DomainError("SOURCE_PREFIX_CAUGHT_UP", "No further source proofs", 409)
                maximum = previous.max_observed_at if previous else None
                proofs = []
                for next_revision in range(
                    revision + 1, min(position.revision, revision + request.limit) + 1
                ):
                    proof = TraceAttestationRepository(db).at_revision(
                        config.consumer_id, next_revision
                    )
                    if proof is None:
                        raise DomainError("CONFLICT", "Source prefix proof is missing", 409)
                    verified = self.proofs.reverify_in_transaction(db, proof.request.id)
                    if verified != proof:
                        raise DomainError("CONFLICT", "Source prefix proof changed", 409)
                    record = UsageTraceRepository(db).get(proof.request.record_id)
                    if record is None or record.before.configuration != config:
                        raise DomainError("CONFLICT", "Source prefix record differs", 409)
                    times = [
                        source.observation.reference.compute_observed_at
                        for source in record.sources
                        if source.observation is not None
                    ]
                    if record.before.trace is not None:
                        times.append(record.before.trace.anchor.reference.compute_observed_at)
                    if maximum is not None:
                        times.append(maximum)
                    maximum = max(times) if times else None
                    proofs.append(proof)
                    if monotonic() >= deadline:
                        raise DomainError(
                            "RETRYABLE_DATABASE_ERROR", "Source prefix budget exceeded", 503
                        )
                draft = StoredSourcePrefix.model_construct(
                    request=request,
                    configuration=config,
                    proofs=tuple(proofs),
                    max_observed_at=maximum,
                    event_id=uuid4(),
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                value = StoredSourcePrefix.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": source_prefix_fingerprint(draft),
                    }
                )
                if self.configuration != config:
                    raise DomainError("CONFLICT", "Source prefix configuration changed", 409)
                action = "lyra.usage.source_prefix_checkpointed"
                payload = {
                    "checkpoint_id": str(request.id),
                    "node_id": config.node_id,
                    "through_revision": value.proofs[-1].revision,
                    "fingerprint": value.fingerprint,
                    "source_deletion_enabled": False,
                }
                db.add(
                    DomainEvent(
                        event_id=str(value.event_id),
                        event_type=action,
                        event_version=1,
                        aggregate_type="usage_source_prefix",
                        aggregate_id=str(request.id),
                        aggregate_version=1,
                        occurred_at=value.created_at,
                        recorded_at=value.created_at,
                        actor=context.actor,
                        correlation_id=context.request_id,
                        causation_id=(
                            str(request.previous.id) if request.previous else str(request.id)
                        ),
                        payload=payload,
                    )
                )
                db.flush()
                repo.append(value)
                db.add(OutboxEntry(event_id=str(value.event_id), created_at=value.created_at))
                db.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=context.actor,
                        action=action,
                        target=str(request.id),
                        request_id=context.request_id,
                        recorded_at=value.created_at,
                        changes=payload,
                    )
                )
                db.flush()
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Source prefix budget exceeded", 503
                    )
                commit_owned_transaction(db)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None
