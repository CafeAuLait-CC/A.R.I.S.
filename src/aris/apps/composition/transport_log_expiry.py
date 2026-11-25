"""Bind Core log expiry to Vega's retained original acknowledgments."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.log_expiry_schemas import (
    LogExpiryRequest,
    LogExpiryResult,
    LogPair,
    LogScope,
    LogTarget,
)
from aris.core.log_expiry_service import CoreLogMaintenance
from aris.modules.vega.payload_retirement_schemas import PayloadRetirementContext
from aris.modules.vega.retired_receipt_service import retired_receipts_in_transaction
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery

TRANSPORT_TYPES = frozenset(("vega.telemetry.accepted", "vega.heartbeat.accepted"))


def normal_transport_pair(pair: LogPair) -> bool:
    """Accepted transport has a preserved projection flag, which can also be False."""
    event, audit = pair.event, pair.audit
    changes = audit.changes
    return (
        event.event_type in TRANSPORT_TYPES
        and event.event_version == 1
        and event.aggregate_type == "node"
        and set(event.payload) == {"message_id", "applied_to_current"}
        and event.payload["message_id"] == str(event.causation_id)
        and type(event.payload["applied_to_current"]) is bool
        and set(changes) == {"previous_seq", "received_seq", "applied_to_current"}
        and type(changes["previous_seq"]) is int
        and changes["previous_seq"] >= 0
        and type(changes["received_seq"]) is int
        and changes["received_seq"] == event.aggregate_version
        and changes["applied_to_current"] is event.payload["applied_to_current"]
        and (audit.actor, audit.action, audit.target, audit.request_id)
        == (event.actor, event.event_type, event.aggregate_id, event.correlation_id)
    )


def retire_transport_logs(
    sessions: sessionmaker[Session],
    context: PayloadRetirementContext,
    operation_id: UUID,
    source: UsageArchiveHeadQuery,
    scope: LogScope,
    pairs: tuple[LogPair, ...],
) -> LogExpiryResult:
    context = PayloadRetirementContext.model_validate(context.model_dump(mode="json"))
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    scope = LogScope.model_validate(scope.model_dump(mode="json"))
    pairs = tuple(LogPair.model_validate(value.model_dump(mode="json")) for value in pairs)
    if (
        not 1 <= len(pairs) <= 1024
        or sum(len(value.model_dump_json()) for value in pairs) > 2 * 1024 * 1024
    ):
        raise ValueError("Transport log expiry exceeds its explicit batch budget")
    if (
        (scope.aggregate_type, scope.aggregate_id, scope.actor)
        != ("node", source.node_id, source.agent_id)
        or not set(scope.event_types) <= TRANSPORT_TYPES
        or any(not normal_transport_pair(pair) for pair in pairs)
    ):
        raise DomainError(
            "VALIDATION_ERROR", "Only original ordinary transport acknowledgments qualify", 422
        )
    first = min(pair.event.aggregate_version for pair in pairs)
    last = max(pair.event.aggregate_version for pair in pairs)
    maintenance = CoreLogMaintenance(sessions, allowed_types=TRANSPORT_TYPES)
    try:
        with sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            transaction_budget(db, 1000, 3000, 15000)
            evidence = retired_receipts_in_transaction(db, source, first=first, last=last)
            originals = {row.seq: row for row in evidence.receipts}
            for pair in pairs:
                original = originals[pair.event.aggregate_version]
                if original.message_id != pair.event.causation_id or (
                    original.applied_to_current is not pair.event.payload["applied_to_current"]
                ):
                    raise DomainError(
                        "CONFLICT", "Transport log differs from its original acknowledgment", 409
                    )
            request = LogExpiryRequest(
                id=operation_id,
                scope=scope,
                source_evidence_fingerprint=evidence.fingerprint,
                targets=tuple(LogTarget.from_pair(pair) for pair in pairs),
            )
            result = maintenance.expire_in_transaction(
                db, request, actor=context.actor, request_id=context.request_id
            )
            commit_owned_transaction(db)
            return result
    except DBAPIError as error:
        foreign_key = (
            getattr(error.orig, "sqlstate", None) == "23503"
            or getattr(error.orig, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_FOREIGNKEY"
        )
        code = "CONFLICT" if foreign_key else database_failure(error)
        raise DomainError(
            code,
            "Historical transport logs remain referenced or unavailable",
            409 if code == "CONFLICT" else 503,
        ) from None
