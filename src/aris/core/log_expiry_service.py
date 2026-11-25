"""Core owns generic log removal; domain evidence must be supplied by composition."""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.log_expiry_repo import LogExpiryRepository, audit_snapshot, event_snapshot
from aris.core.log_expiry_schemas import (
    LogExpiryRequest,
    LogExpiryResult,
    LogPair,
    LogScope,
    digest,
)
from aris.core.time import utc

ACTION = "core.history.logs_retired"


class CoreLogMaintenance:
    def __init__(self, sessions: sessionmaker[Session], *, allowed_types: frozenset[str]) -> None:
        if not allowed_types:
            raise ValueError("Log maintenance requires an explicit trusted event policy")
        self.sessions, self.allowed_types = sessions, allowed_types

    def candidates(
        self, scope: LogScope, *, audit_sequence_key: str, maximum: int
    ) -> Iterator[LogPair]:
        if (
            not 1 <= maximum <= 200000
            or not audit_sequence_key
            or not set(scope.event_types) <= self.allowed_types
        ):
            raise ValueError("Log candidate policy or budget differs")
        with self.sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                db.execute(text("SET LOCAL statement_timeout='60s'"))
                db.execute(text("SET LOCAL transaction_timeout='10min'"))
            yield from LogExpiryRepository(db).pairs(
                scope, audit_sequence_key=audit_sequence_key, maximum=maximum
            )

    def completed_in_transaction(
        self, db: Session, request: LogExpiryRequest, *, actor: str
    ) -> LogExpiryResult | None:
        previous = db.get(AuditEntry, str(request.id))
        if previous is None:
            return None
        repo = LogExpiryRepository(db)
        if (
            previous.actor != actor
            or previous.action != ACTION
            or previous.changes.get("request_fingerprint") != request.fingerprint
            or repo.remaining(
                tuple(str(row.event_id) for row in request.targets),
                tuple(str(row.audit_id) for row in request.targets),
            )
        ):
            raise DomainError("CONFLICT", "Log expiry differs from its saved operation", 409)
        return LogExpiryResult(
            id=request.id, events=len(request.targets), audits=len(request.targets), duplicate=True
        )

    def expire_in_transaction(
        self, db: Session, request: LogExpiryRequest, *, actor: str, request_id: str
    ) -> LogExpiryResult:
        request = LogExpiryRequest.model_validate(request.model_dump(mode="json"))
        if (
            request.scope.before > datetime.now(UTC)
            or not set(request.scope.event_types) <= self.allowed_types
        ):
            raise DomainError("VALIDATION_ERROR", "Log expiry policy or cutoff differs", 422)
        completed = self.completed_in_transaction(db, request, actor=actor)
        if completed is not None:
            return completed
        ids = tuple(str(row.event_id) for row in request.targets)
        audit_ids = tuple(str(row.audit_id) for row in request.targets)
        repo = LogExpiryRepository(db)
        outbox, events, audits = repo.locked(ids, audit_ids)
        if (
            len(outbox) != len(ids)
            or len(events) != len(ids)
            or len(audits) != len(ids)
            or any(row.published_at is not None for row in outbox)
            or repo.referenced_publication(ids)
        ):
            raise DomainError("CONFLICT", "Log records are missing, published or referenced", 409)
        current_events = {row.event_id: row for row in events}
        current_audits = {row.id: row for row in audits}
        scope = request.scope
        for target in request.targets:
            event = event_snapshot(current_events[str(target.event_id)])
            audit = audit_snapshot(current_audits[str(target.audit_id)])
            if (
                digest(event) != target.event_fingerprint
                or digest(audit) != target.audit_fingerprint
                or event.event_type not in scope.event_types
                or event.event_version != 1
                or event.aggregate_type != scope.aggregate_type
                or event.aggregate_id != scope.aggregate_id
                or event.actor != scope.actor
                or not 1 <= event.aggregate_version <= scope.through_seq
                or max(event.occurred_at, event.recorded_at, audit.recorded_at) >= scope.before
                or (audit.actor, audit.action, audit.target, audit.request_id)
                != (event.actor, event.event_type, event.aggregate_id, event.correlation_id)
            ):
                raise DomainError("CONFLICT", "Original log record or scope changed", 409)
        # A new or unrecognized foreign-key reference still prevents removal at
        # the database boundary. The owner must roll the complete transaction back.
        repo.delete(ids, audit_ids)
        db.add(
            AuditEntry(
                id=str(request.id),
                actor=actor,
                action=ACTION,
                target=scope.aggregate_id,
                request_id=request_id,
                recorded_at=datetime.now(UTC),
                changes={
                    "request_fingerprint": request.fingerprint,
                    "source_evidence_fingerprint": request.source_evidence_fingerprint,
                    "events": len(ids),
                    "audits": len(audit_ids),
                    "before": scope.before.isoformat(),
                    "first_event_occurred_at": min(
                        utc(row.occurred_at) for row in events
                    ).isoformat(),
                    "last_event_occurred_at": max(
                        utc(row.occurred_at) for row in events
                    ).isoformat(),
                    "first_aggregate_version": min(row.aggregate_version for row in events),
                    "last_aggregate_version": max(row.aggregate_version for row in events),
                },
            )
        )
        db.flush()
        return LogExpiryResult(
            id=request.id, events=len(ids), audits=len(audit_ids), duplicate=False
        )
