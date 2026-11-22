"""Persist bounded candidate trace progress; no daemon, session or charge activation."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.residency_state import (
    ClosedResidencyCandidate,
    ResidencyFormationCheckpoint,
    advance_residency_formation,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository, inconsistent_trace
from aris.modules.lyra.trace_consumer_schemas import (
    MAX_CLOSED_RESIDENCIES,
    MAX_TRACE_RECORD_BYTES,
    ConsumedTraceSource,
    StoredTraceConsumption,
    TraceConsumptionIdle,
    TraceConsumptionReceipt,
    UsageTraceConfiguration,
    UsageTraceConsumeContext,
    UsageTraceConsumeRequest,
    UsageTracePosition,
    canonical_trace,
    consumption_receipt,
    trace_fingerprint,
)
from aris.modules.lyra.trace_state import (
    UsageTraceCheckpoint,
    advance_usage_trace,
    observation_stamp,
)
from aris.modules.vega.usage_archive_schemas import (
    ArchivedUsageItem,
    UsageArchivePage,
    UsageArchiveQuery,
)
from aris.modules.vega.usage_schemas import UsageObservationFact, UsageObservationQuery


class TraceArchiveReader(Protocol):
    def page_in_transaction(
        self, session: Session, query: UsageArchiveQuery
    ) -> UsageArchivePage: ...


class TraceObservationReader(Protocol):
    def observation_in_transaction(
        self, session: Session, query: UsageObservationQuery
    ) -> UsageObservationFact | None: ...


@dataclass(frozen=True)
class _TraceEvaluation:
    trace: UsageTraceCheckpoint | None
    residency: ResidencyFormationCheckpoint | None
    transitions_fingerprint: str
    sources: tuple[ConsumedTraceSource, ...]
    closed_runs: tuple[ClosedResidencyCandidate, ...]


@dataclass(frozen=True)
class _ReplayInput:
    before: UsageTracePosition
    items: tuple[ArchivedUsageItem, ...]
    anchor: UsageObservationFact | None
    source_cutoff: datetime | None


_REPLAY_SCOPE = "aris.lyra.trace_replay"
_MAX_REPLAYS = 4


@dataclass
class _ReplayScope:
    transaction: SessionTransaction
    results: dict[tuple[str, datetime | None], tuple[_ReplayInput, _TraceEvaluation]] = field(
        default_factory=dict
    )


def _active_replay_scope(session: Session) -> _ReplayScope | None:
    scope = session.info.get(_REPLAY_SCOPE)
    if isinstance(scope, _ReplayScope) and scope.transaction is session.get_transaction():
        return scope
    return None


@contextmanager
def trace_replay_scope(session: Session) -> Iterator[None]:
    transaction = session.get_transaction()
    if transaction is None or _active_replay_scope(session) is not None:
        yield
        return
    previous = session.info.get(_REPLAY_SCOPE)
    session.info[_REPLAY_SCOPE] = _ReplayScope(transaction)
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(_REPLAY_SCOPE, None)
        else:
            session.info[_REPLAY_SCOPE] = previous


class UsageTraceConsumerService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        archive: TraceArchiveReader,
        observations: TraceObservationReader,
        *,
        configuration: UsageTraceConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.archive, self.observations = sessions, archive, observations
        self.configuration = (
            UsageTraceConfiguration.model_validate(configuration.model_dump(mode="json"))
            if configuration is not None
            else None
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("trace consumer clock requires a timezone")
        return now.astimezone(UTC)

    def _configuration(self, expected: str | None = None) -> UsageTraceConfiguration:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Candidate processing is not configured", 503)
        configuration = UsageTraceConfiguration.model_validate(
            self.configuration.model_dump(mode="json")
        )
        if expected is not None and configuration.fingerprint != expected:
            raise DomainError("CONFLICT", "Candidate processing configuration differs", 409)
        return configuration

    def position(self) -> UsageTracePosition:
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, 3000)
                return UsageTraceRepository(session).position(self._configuration())
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Candidate position database is unavailable",
                503,
            ) from None

    def exact(self, record_id: UUID) -> StoredTraceConsumption:
        with self.sessions() as session, session.no_autoflush:
            record = UsageTraceRepository(session).get(record_id)
            if record is None:
                raise DomainError("NOT_FOUND", "Candidate consumption record not found", 404)
            return record

    def _page(self, session: Session, query: UsageArchiveQuery) -> UsageArchivePage:
        page = self.archive.page_in_transaction(session, query)
        page = UsageArchivePage.model_validate(page.model_dump(mode="json"))
        if page.query != query:
            raise DomainError("CONFLICT", "Candidate archive page scope differs", 409)
        return page

    def _evaluate(
        self,
        session: Session,
        before: UsageTracePosition,
        page: UsageArchivePage,
        *,
        source_cutoff: datetime | None = None,
    ) -> _TraceEvaluation:
        checkpoint, anchor = before.trace, None
        if checkpoint is not None and any(item.observation is not None for item in page.items):
            ref = checkpoint.anchor.reference
            anchor = self.observations.observation_in_transaction(
                session,
                UsageObservationQuery(
                    agent_id=ref.agent_id,
                    node_id=ref.node_id,
                    message_id=ref.message_id,
                    expected_seq=ref.seq,
                ),
            )
            if anchor is None or observation_stamp(anchor) != checkpoint.anchor:
                raise DomainError(
                    "CONFLICT", "Original candidate trace anchor is unavailable or changed", 409
                )
            if (
                source_cutoff is not None
                and max(anchor.observed_at, anchor.received_at) > source_cutoff
            ):
                raise DomainError("CONFLICT", "Original trace anchor follows the cutoff", 409)
        inputs = _ReplayInput(before, page.items, anchor, source_cutoff)
        scope = _active_replay_scope(session)
        if scope is None:
            return self._replay(inputs)
        # Page and anchor were read and validated again above. Only reuse the
        # computation when all the current inputs match within this transaction.
        key = (page.fingerprint, source_cutoff)
        previous = scope.results.get(key)
        if previous is not None and previous[0] == inputs:
            return previous[1]
        result = self._replay(inputs)
        if len(scope.results) < _MAX_REPLAYS:
            scope.results[key] = (inputs, result)
        return result

    @staticmethod
    def _replay(inputs: _ReplayInput) -> _TraceEvaluation:
        before, source_cutoff = inputs.before, inputs.source_cutoff
        checkpoint, residency = before.trace, before.residency
        cache: dict[UUID, UsageObservationFact] = {}
        if inputs.anchor is not None:
            cache[inputs.anchor.message_id] = inputs.anchor
        sources: list[ConsumedTraceSource] = []
        closed: list[ClosedResidencyCandidate] = []
        transitions = trace_fingerprint("transitions", "[]")
        for item in inputs.items:
            if (
                source_cutoff is not None
                and max(item.reference.observed_at, item.reference.received_at) > source_cutoff
            ):
                raise DomainError("CONFLICT", "Original trace source follows the cutoff", 409)
            fact = item.observation
            sources.append(
                ConsumedTraceSource(
                    reference=item.reference,
                    observation=observation_stamp(fact) if fact is not None else None,
                )
            )
            if fact is None:
                continue
            anchor = (
                cache.get(checkpoint.anchor.reference.message_id)
                if checkpoint is not None
                else None
            )
            step = advance_usage_trace(
                checkpoint, fact, rule=before.configuration.continuity, anchor=anchor
            )
            if step.requires_rebuild:
                raise DomainError("CONFLICT", "Candidate source order requires reconstruction", 409)
            formed = advance_residency_formation(
                residency,
                step,
                continuity=before.configuration.continuity,
                formation=before.configuration.formation,
            )
            residency = formed.checkpoint
            closed.extend(formed.closed)
            if len(closed) > MAX_CLOSED_RESIDENCIES:
                raise DomainError(
                    "USAGE_PROCESSING_TOO_LARGE", "Candidate page requires a smaller batch", 503
                )
            transitions = trace_fingerprint(
                "transition",
                json.dumps(
                    {
                        "previous": transitions,
                        "trace": step.model_dump(mode="json"),
                        "residency": formed.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            )
            cache[fact.message_id] = fact
            checkpoint = step.checkpoint
        return _TraceEvaluation(
            trace=checkpoint,
            residency=residency,
            transitions_fingerprint=transitions,
            sources=tuple(sources),
            closed_runs=tuple(closed),
        )

    def _verify_sources(
        self,
        session: Session,
        record: StoredTraceConsumption,
        *,
        source_cutoff: datetime | None = None,
    ) -> None:
        before, config = record.before, record.before.configuration
        page = self._page(
            session,
            UsageArchiveQuery(
                agent_id=config.agent_id,
                node_id=config.node_id,
                after_seq=before.after_seq,
                through_seq=record.position.after_seq,
                limit=len(record.sources),
            ),
        )
        evaluated = self._evaluate(session, before, page, source_cutoff=source_cutoff)
        if (
            page.state != "range_end"
            or evaluated.sources != record.sources
            or evaluated.trace != record.after_trace
            or evaluated.residency != record.after_residency
            or evaluated.closed_runs != record.closed_runs
            or evaluated.transitions_fingerprint != record.transitions_fingerprint
        ):
            raise DomainError("CONFLICT", "Candidate source prefix changed during processing", 409)

    def verify_record_in_transaction(
        self,
        session: Session,
        record_id: UUID,
        expected_fingerprint: str,
        source_cutoff: datetime,
    ) -> StoredTraceConsumption:
        """Reproduce one bounded record from originals, not its entire prior history.

        The caller owns the transaction budget and must independently establish
        the authority of the record's starting checkpoint.
        """
        with session.no_autoflush:
            record = UsageTraceRepository(session).get(record_id)
            if record is None:
                raise DomainError("NOT_FOUND", "Candidate consumption record not found", 404)
            if record.fingerprint != expected_fingerprint:
                raise DomainError("CONFLICT", "Candidate consumption fingerprint differs", 409)
            if record.created_at > source_cutoff:
                raise DomainError("CONFLICT", "Candidate record follows the source cutoff", 409)
            self._verify_sources(session, record, source_cutoff=source_cutoff)
            return record

    def consume(
        self, context: UsageTraceConsumeContext, request: UsageTraceConsumeRequest
    ) -> TraceConsumptionReceipt | TraceConsumptionIdle:
        with self.sessions() as session:
            try:
                transaction_budget(session, 1000, 1000, 10000)
            except DBAPIError as error:
                code = database_failure(error)
                raise DomainError(
                    code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                    "Candidate processing database is unavailable",
                    503,
                ) from None
            result = self.consume_in_transaction(session, context, request)
            commit_owned_transaction(session)
            return result

    def consume_in_transaction(
        self, session: Session, context: UsageTraceConsumeContext, request: UsageTraceConsumeRequest
    ) -> TraceConsumptionReceipt | TraceConsumptionIdle:
        context = UsageTraceConsumeContext.model_validate(context.model_dump(mode="json"))
        request = UsageTraceConsumeRequest.model_validate(request.model_dump(mode="json"))
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = UsageTraceRepository(session)
                repo.lock("operation", str(request.id))
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.request != request or prior.context.actor != context.actor:
                        raise DomainError(
                            "CONFLICT", "Candidate operation has different input", 409
                        )
                    current = repo.position(prior.before.configuration)
                    if current.revision < prior.position.revision:
                        raise inconsistent_trace()
                    return consumption_receipt(prior, duplicate=True)
                configuration = self._configuration(request.expected_configuration_fingerprint)
                repo.lock("consumer", configuration.consumer_id)
                if self._configuration(request.expected_configuration_fingerprint) != configuration:
                    raise DomainError(
                        "CONFLICT", "Candidate configuration changed while waiting", 409
                    )
                before = repo.position(configuration)
                if (
                    before.revision != request.expected_revision
                    or request.through_seq < before.after_seq
                ):
                    raise DomainError(
                        "CONFLICT", "Candidate position differs; reload the cursor", 409
                    )
                if request.through_seq == before.after_seq:
                    return TraceConsumptionIdle(
                        id=request.id, state="caught_up", position=before, missing_seq=None
                    )
                page = self._page(
                    session,
                    UsageArchiveQuery(
                        agent_id=configuration.agent_id,
                        node_id=configuration.node_id,
                        after_seq=before.after_seq,
                        through_seq=request.through_seq,
                        limit=request.limit,
                    ),
                )
                if not page.items:
                    return TraceConsumptionIdle(
                        id=request.id,
                        state="gap",
                        position=before,
                        missing_seq=before.after_seq + 1,
                    )
                evaluated = self._evaluate(session, before, page)
                now = self._now()
                if before.recorded_at is not None and now < before.recorded_at:
                    raise DomainError(
                        "CONFLICT", "Candidate trace clock moved before its prefix", 409
                    )
                unchecked = StoredTraceConsumption.model_construct(
                    request=request,
                    context=context,
                    event_id=uuid4(),
                    before=before,
                    sources=evaluated.sources,
                    after_trace=evaluated.trace,
                    after_residency=evaluated.residency,
                    closed_runs=evaluated.closed_runs,
                    transitions_fingerprint=evaluated.transitions_fingerprint,
                    page_state=page.state,
                    missing_seq=page.missing_seq,
                    created_at=now,
                    fingerprint="0" * 64,
                )
                if len(canonical_trace(unchecked).encode()) > MAX_TRACE_RECORD_BYTES:
                    raise DomainError(
                        "USAGE_PROCESSING_TOO_LARGE", "Candidate page requires a smaller batch", 503
                    )
                record = StoredTraceConsumption.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": trace_fingerprint(
                            "record", canonical_trace(unchecked, exclude={"fingerprint"})
                        ),
                    }
                )
                self._verify_sources(session, record)
                self._append(repo, record)
                session.flush()
                self._verify_sources(session, record)
                if self._configuration(request.expected_configuration_fingerprint) != configuration:
                    raise DomainError(
                        "CONFLICT", "Candidate configuration changed before recording", 409
                    )
                position = repo.position(configuration)
                if position != record.position:
                    raise inconsistent_trace()
                stored = repo.get(request.id)
                if stored != record:
                    raise inconsistent_trace()
                return consumption_receipt(record, duplicate=False)
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Candidate processing database is unavailable",
                503,
            ) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_trace() from None

    @staticmethod
    def _append(repo: UsageTraceRepository, record: StoredTraceConsumption) -> None:
        session, before, position = repo.session, record.before, record.position
        event_id = str(record.event_id)
        config = before.configuration
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type="lyra.usage.trace_consumed",
                event_version=1,
                aggregate_type="usage_trace",
                aggregate_id=config.consumer_id,
                aggregate_version=position.revision,
                occurred_at=record.created_at,
                recorded_at=record.created_at,
                actor=record.context.actor,
                correlation_id=record.context.request_id,
                causation_id=str(record.request.id),
                payload={
                    "consumer_id": config.consumer_id,
                    "node_id": config.node_id,
                    "first_seq": before.after_seq + 1,
                    "last_seq": position.after_seq,
                    "source_count": len(record.sources),
                    "fingerprint": record.fingerprint,
                    "configuration_fingerprint": config.fingerprint,
                    "candidate_only": True,
                },
            )
        )
        session.flush()
        repo.append(record)
        session.add(OutboxEntry(event_id=event_id, created_at=record.created_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=record.context.actor,
                action="lyra.usage.trace_consumed",
                target=config.consumer_id,
                request_id=record.context.request_id,
                recorded_at=record.created_at,
                changes={
                    "record_id": str(record.request.id),
                    "fingerprint": record.fingerprint,
                    "previous_revision": before.revision,
                    "revision": position.revision,
                    "previous_seq": before.after_seq,
                    "after_seq": position.after_seq,
                    "candidate_only": True,
                },
            )
        )
