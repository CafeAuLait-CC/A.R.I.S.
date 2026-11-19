"""Preserve bounded rebuilt candidates separately from formal ledger measurements."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingContext
from aris.modules.lyra.residency_segments import ResidencySegmentAnalysis, ResidencySegmentQuery
from aris.modules.lyra.segment_store_repo import ResidencySegmentRepository, inconsistent_segments
from aris.modules.lyra.segment_store_schemas import (
    RecordResidencySegments,
    ResidencySegmentLookup,
    ResidencySegmentPage,
    SegmentSetReceipt,
    StoredResidencySegmentSet,
    segment_page_fingerprint,
    segment_set_fingerprint,
)


class ResidencySegmentAnalyzer(Protocol):
    def analyze_in_transaction(
        self, session: Session, query: ResidencySegmentQuery
    ) -> ResidencySegmentAnalysis: ...


class ResidencySegmentStore:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        analyzer: ResidencySegmentAnalyzer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.analyzer = sessions, analyzer
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("segment store clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Segment evidence budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Residency segment storage is unavailable",
            503,
        )

    def exact(self, id: UUID) -> StoredResidencySegmentSet:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 10000)
                value = ResidencySegmentRepository(session).get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Saved residency segments not found", 404)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify(self, id: UUID) -> StoredResidencySegmentSet:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 10000)
                return self.reverify_in_transaction(session, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def reverify_in_transaction(self, session: Session, id: UUID) -> StoredResidencySegmentSet:
        deadline = monotonic() + 10
        try:
            with session.no_autoflush:
                repo = ResidencySegmentRepository(session)
                value = repo.get(id)
                if value is None:
                    raise DomainError("NOT_FOUND", "Saved residency segments not found", 404)
                if value.created_at > self._now():
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Saved segments are not reached", 503
                    )
                result = self.analyzer.analyze_in_transaction(session, value.request.source)
                result = ResidencySegmentAnalysis.model_validate(result.model_dump(mode="json"))
                if (
                    result.query != value.request.source
                    or result.fingerprint != value.analysis.fingerprint
                ):
                    raise DomainError(
                        "CONFLICT", "Original residency segments differ from saved evidence", 409
                    )
                repo.verify_index(value)
                if repo.get(id) != value:
                    raise inconsistent_segments()
                self._deadline(deadline)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_segments() from None

    def record(
        self, context: UsagePostingContext, request: RecordResidencySegments
    ) -> SegmentSetReceipt:
        with self.sessions() as session:
            try:
                transaction_budget(session, 1000, 1000, 30000)
                result = self.record_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
            except DBAPIError as error:
                raise self._failure(error) from None

    def record_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordResidencySegments
    ) -> SegmentSetReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = RecordResidencySegments.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + 30
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = ResidencySegmentRepository(session)
                repo.lock("operation", str(request.id))
                previous = repo.get(request.id)
                if previous is not None:
                    if previous.request != request:
                        raise DomainError(
                            "CONFLICT", "Segment recording ID has different input", 409
                        )
                    return SegmentSetReceipt(value=previous, duplicate=True)
                repo.lock("source", str(request.source.attestation_id))
                if repo.get(request.source.attestation_id, by_attestation=True) is not None:
                    raise DomainError(
                        "CONFLICT", "Source attestation already has saved residency segments", 409
                    )
                analysis = self.analyzer.analyze_in_transaction(session, request.source)
                analysis = ResidencySegmentAnalysis.model_validate(analysis.model_dump(mode="json"))
                if analysis.query != request.source:
                    raise DomainError("CONFLICT", "Residency segment source binding differs", 409)
                unchecked = StoredResidencySegmentSet.model_construct(
                    request=request,
                    event_id=uuid4(),
                    analysis=analysis,
                    created_at=self._now(),
                    fingerprint="0" * 64,
                )
                value = StoredResidencySegmentSet.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": segment_set_fingerprint(unchecked),
                    }
                )
                self._emit(session, context, value)
                session.flush()
                repo.append_set(value)
                session.flush()
                repo.append_index(value)
                session.flush()
                if self.reverify_in_transaction(session, request.id) != value:
                    raise inconsistent_segments()
                self._deadline(deadline)
                return SegmentSetReceipt(value=value, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_segments() from None

    def lookup(self, query: ResidencySegmentLookup) -> ResidencySegmentPage:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 10000)
                return self.lookup_in_transaction(session, query)
        except DBAPIError as error:
            raise self._failure(error) from None

    def lookup_in_transaction(
        self, session: Session, query: ResidencySegmentLookup
    ) -> ResidencySegmentPage:
        query = ResidencySegmentLookup.model_validate(query.model_dump(mode="json"))
        deadline = monotonic() + 10
        try:
            with session.no_autoflush:
                items = ResidencySegmentRepository(session).lookup(query)
                expected = (
                    min(items[-1].segment.run_at_segment_end.intervals, query.through_interval)
                    if items
                    else query.after_interval
                )
                state: Literal["range_end", "page_end", "gap"] = (
                    "range_end"
                    if expected == query.through_interval
                    else ("page_end" if len(items) == query.limit else "gap")
                )
                unchecked = ResidencySegmentPage.model_construct(
                    query=query,
                    items=items,
                    next_after_interval=expected,
                    state=state,
                    missing_interval=expected if state == "gap" else None,
                    read_at=self._now(),
                    fingerprint="0" * 64,
                )
                result = ResidencySegmentPage.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": segment_page_fingerprint(unchecked),
                    }
                )
                self._deadline(deadline)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_segments() from None

    @staticmethod
    def _emit(
        session: Session, context: UsagePostingContext, value: StoredResidencySegmentSet
    ) -> None:
        action = "lyra.usage.residency_segments_recorded"
        event_id = str(value.event_id)
        payload = {
            "record_id": str(value.request.id),
            "attestation_id": str(value.request.source.attestation_id),
            "consumer_id": value.analysis.attestation.consumer_id,
            "segment_count": len(value.analysis.segments),
            "fingerprint": value.fingerprint,
            "quota_usage_available": False,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=action,
                event_version=1,
                aggregate_type="usage_residency_segments",
                aggregate_id=str(value.request.id),
                aggregate_version=1,
                occurred_at=value.created_at,
                recorded_at=value.created_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.request.source.attestation_id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=value.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=str(value.request.id),
                request_id=context.request_id,
                recorded_at=value.created_at,
                changes=payload,
            )
        )
