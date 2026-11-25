"""Explicit local maintenance only; the coordinator must verify consumer dependencies."""

from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.modules.vega.payload_retirement_repo import PayloadRetirementRepository
from aris.modules.vega.payload_retirement_schemas import (
    PayloadRetirementContext,
    PayloadRetirementReceipt,
    PayloadRetirementRequest,
)
from aris.modules.vega.usage_archive_schemas import UsageArchiveQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive

ACTION = "vega.archive.payloads_retired"


class VegaPayloadMaintenance:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def completed_in_transaction(
        self, session: Session, context: PayloadRetirementContext, request: PayloadRetirementRequest
    ) -> PayloadRetirementReceipt | None:
        """Replay a completed operation before asking for a new maintenance assessment."""
        request = PayloadRetirementRequest.model_validate(request.model_dump(mode="json"))
        context = PayloadRetirementContext.model_validate(context.model_dump(mode="json"))
        existing = session.get(AuditEntry, str(request.id))
        if existing is None:
            return None
        repo = PayloadRetirementRepository(session)
        boundary = repo.lock_source(request.source)
        first, last = request.reports[0].seq, request.reports[-1].seq
        expected = tuple((row.seq, str(row.message_id), row.fingerprint) for row in request.reports)
        if (
            existing.actor != context.actor
            or existing.action != ACTION
            or existing.target != request.source.node_id
            or existing.changes.get("batch_fingerprint") != request.fingerprint
            or repo.original_receipts(request.source.agent_id, first, last) != expected
            or repo.original_rows(request.source, first, last)
            or boundary.retired_through_seq < last
        ):
            raise DomainError("CONFLICT", "Retirement operation differs from its saved batch", 409)
        return PayloadRetirementReceipt(
            id=request.id,
            source=request.source,
            first_seq=first,
            through_seq=last,
            retired_payloads=len(request.reports),
            fingerprint=request.fingerprint,
            duplicate=True,
        )

    def retire(
        self, context: PayloadRetirementContext, request: PayloadRetirementRequest
    ) -> PayloadRetirementReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 3000, 15000)
                receipt = self.retire_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return receipt
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Archive retirement is unavailable", 503
            ) from None

    def retire_in_transaction(
        self, session: Session, context: PayloadRetirementContext, request: PayloadRetirementRequest
    ) -> PayloadRetirementReceipt:
        request = PayloadRetirementRequest.model_validate(request.model_dump(mode="json"))
        context = PayloadRetirementContext.model_validate(context.model_dump(mode="json"))
        if request.before > datetime.now(UTC):
            raise DomainError("VALIDATION_ERROR", "Retirement cutoff must already be closed", 422)
        repo = PayloadRetirementRepository(session)
        boundary = repo.lock_source(request.source)
        first, last = request.reports[0].seq, request.reports[-1].seq
        completed = self.completed_in_transaction(session, context, request)
        if completed is not None:
            return completed
        expected = tuple((row.seq, str(row.message_id), row.fingerprint) for row in request.reports)
        if last >= boundary.protected_seq:
            raise DomainError("CONFLICT", "Retirement would remove a current fact source", 409)
        if boundary.retired_through_seq != first - 1:
            raise DomainError("CONFLICT", "Retirement is not the verified oldest prefix", 409)
        archive = VegaUsageArchive(self.sessions)
        prefix = archive.retention_prefix_in_transaction(
            session,
            request.source,
            start=datetime(1970, 1, 1, tzinfo=UTC),
            end=request.before,
            through_seq=last,
        )
        if (prefix.first_seq, prefix.through_seq) != (first, last):
            raise DomainError("CONFLICT", "Retirement is not the verified oldest prefix", 409)
        reports = archive.reporting_page_in_transaction(
            session,
            UsageArchiveQuery(
                **request.source.model_dump(),
                after_seq=first - 1,
                through_seq=last,
            ),
        ).reports
        if reports != request.reports:
            raise DomainError("CONFLICT", "Retirement original reports changed", 409)
        rows = repo.original_rows(request.source, first, last)
        if tuple((row.seq, row.message_id, row.fingerprint) for row in rows) != expected:
            raise DomainError("CONFLICT", "Retirement row identities changed", 409)
        repo.replace(request.source, rows)
        session.add(
            AuditEntry(
                id=str(request.id),
                actor=context.actor,
                action=ACTION,
                target=request.source.node_id,
                request_id=context.request_id,
                recorded_at=datetime.now(UTC),
                changes={
                    "agent_id": request.source.agent_id,
                    "first_seq": first,
                    "through_seq": last,
                    "retired_payloads": len(rows),
                    "batch_fingerprint": request.fingerprint,
                    "dependency_fingerprint": request.dependency_fingerprint,
                    "before": request.before.isoformat(),
                },
            )
        )
        session.flush()
        return PayloadRetirementReceipt(
            id=request.id,
            source=request.source,
            first_seq=first,
            through_seq=last,
            retired_payloads=len(request.reports),
            fingerprint=request.fingerprint,
            duplicate=False,
        )
